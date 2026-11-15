"""Authentication: who is calling.

The API trusts a signed JWT (RS256, checked against the identity provider's public keys) and nothing else. The `sub`
claim is the customer id (`user_id`), and it is what the tools, the memory thread, the tickets and the logs use.
The LLM never sees it or chooses it.

    401  no token / bad signature / expired / wrong iss, aud / unknown kid / revoked / malformed
    403  valid token but the wrong role or the required scope is missing
    429  too many requests from this caller

How a token is checked, in this order (each failure is a 401 with the same generic body; the reason goes to the
`auth.audit` log, never the token or its claims):
  1. The header must say `alg: RS256` (the only algorithm accepted: there is no HS256 path and no shared secret, so
     there is no algorithm-confusion attack to make), must carry a `kid`, and must not carry `jku`, `jwk`, `x5u`
     or `x5c` (a token never tells the API where to find its own key).
  2. The `iss` must be exactly one of the two configured issuers. That only chooses WHICH public keys to use;
     the signature is then checked against that issuer's keys alone.
  3. Signature, `exp`, `iat`, `iss`, `aud`, `sub` and `jti` are required and verified. The token may not live longer
     than JWT_MAX_LIFETIME_SECONDS.
  4. The `jti` must not be in the denylist (revoked by an operator).

Two roles, and each has its own issuer, audience and keys (in Keycloak, two realms). The role comes from the issuer
that signed the token; a `role` claim, if present, must agree. A token without one is a merchant (least privilege).
    merchant  a customer. Uses /chat and /conversations, never /operator/*.
    operator  support staff. Uses /operator/*, never /chat. Its `sub` is the operator id used in the audit trail.
A merchant token on /operator/* is a valid merchant token and gets a 403. It cannot be turned into an operator token by editing
its claims: that breaks the signature, and the operator keys are another issuer's.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_settings
from app.jwks import JwksCache, JwksUnavailable, UnknownKid
from app.ratelimit import RateLimiter
from app.revocation import Denylist
from app.schemas import USER_ID_CHARS, USER_ID_MAX

log = logging.getLogger("auth")
audit = logging.getLogger("auth.audit")  # one line per refused request; never the token or its claims

ALGORITHMS = ["RS256"]  # the whole list, fixed in code: the algorithm never comes from the token
REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub", "jti"]
FORBIDDEN_HEADERS = ("jku", "jwk", "x5u", "x5c")  # ways a token could point to its own key
KID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,128}")
LEEWAY_SECONDS = 5
ROLE_MERCHANT, ROLE_OPERATOR = "merchant", "operator"
ROLES = (ROLE_MERCHANT, ROLE_OPERATOR)

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    """The authenticated caller."""

    user_id: str  # the merchant id, or the operator id when role == "operator"
    scopes: frozenset[str]
    role: str = ROLE_MERCHANT
    jti: str = field(default="", compare=False)  # the token id, for the audit trail and for revocation


@dataclass(frozen=True)
class Realm:
    """One issuer: who signs, for which audience, where its public keys are, and which role it stands for."""

    role: str
    issuer: str
    audience: str
    jwks_url: str


def realms() -> dict[str, Realm]:
    """The two accepted issuers, by issuer string."""
    s = get_settings()
    merchant = Realm(ROLE_MERCHANT, s.jwt_issuer, s.jwt_audience, s.jwks_url)
    operator = Realm(ROLE_OPERATOR, s.operator_jwt_issuer, s.operator_jwt_audience, s.operator_jwks_url)
    return {merchant.issuer: merchant, operator.issuer: operator}


def validate_auth_config() -> None:
    """Refuse to run with a missing or muddled auth setup. Raises RuntimeError (startup, and again on every decode)."""
    s = get_settings()
    problem = None
    names = {"JWKS_URL": s.jwks_url, "JWT_ISSUER": s.jwt_issuer, "JWT_AUDIENCE": s.jwt_audience,
             "OPERATOR_JWKS_URL": s.operator_jwks_url, "OPERATOR_JWT_ISSUER": s.operator_jwt_issuer,
             "OPERATOR_JWT_AUDIENCE": s.operator_jwt_audience}
    missing = [k for k, v in names.items() if not v.strip()]
    if missing:
        problem = f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set"
    elif s.jwt_issuer == s.operator_jwt_issuer or s.jwt_audience == s.operator_jwt_audience:
        problem = "merchants and operators must have different issuers and different audiences"
    elif any(urlsplit(u).scheme not in ("http", "https") or not urlsplit(u).netloc for u in (s.jwks_url, s.operator_jwks_url)):
        problem = "JWKS_URL and OPERATOR_JWKS_URL must be http(s) URLs"
    elif s.jwt_max_lifetime_seconds <= 0:
        problem = "JWT_MAX_LIFETIME_SECONDS must be positive"
    if problem:
        raise RuntimeError(f"Auth configuration: {problem}. The API verifies RS256 tokens against the identity "
                           "provider's JWKS (see .env.example); it has no shared secret to fall back on.")


# ---- process-wide state: key caches, rate limiters and the denylist. `reset_auth_state` is for the tests. ----
_caches: dict[str, JwksCache] = {}
_limiters: dict[str, RateLimiter] = {}
_denylist = Denylist()


def reset_auth_state() -> None:
    """Back to a clean, in-memory state (the tests, and the shutdown of the API)."""
    global _denylist
    _caches.clear()
    _limiters.clear()
    _denylist.close()
    _denylist = Denylist()


def open_persistent_denylist(path: str) -> int:
    """Use the SQLite file at `path` for revocations (the API does this at startup). Returns how many entries are alive."""
    global _denylist
    _denylist.close()
    _denylist = Denylist(path)
    return _denylist.count()


def _cache(realm: Realm) -> JwksCache:
    cache = _caches.get(realm.jwks_url)
    if cache is None:
        s = get_settings()
        cache = _caches[realm.jwks_url] = JwksCache(realm.jwks_url, ttl=s.jwks_cache_ttl_seconds,
                                                    min_refresh=s.jwks_min_refresh_seconds, timeout=s.jwks_timeout_seconds)
    return cache


def _limiter(role: str) -> RateLimiter:
    limiter = _limiters.get(role)
    if limiter is None:
        s = get_settings()
        limiter = _limiters[role] = RateLimiter(s.operator_rate_limit_per_minute if role == ROLE_OPERATOR
                                                else s.rate_limit_per_minute)
    return limiter


def revoke_token(jti: str) -> None:
    """Refuse this token id from now on (for as long as a token could still be alive)."""
    _denylist.revoke(jti, get_settings().jwt_max_lifetime_seconds + LEEWAY_SECONDS)


# ---- audit log: a fixed vocabulary of reasons plus where it came from. Nothing the caller wrote goes in raw. ----

def _printable(value: object, limit: int = 100) -> str:
    return re.sub(r"[^\x21-\x7e]", "?", str(value))[:limit] or "-"


def _where(request: Request | None) -> str:
    if request is None:
        return "-"
    host = request.client.host if request.client else "-"
    return f"method={_printable(request.method, 8)} path={_printable(request.url.path)} client={_printable(host, 45)}"


def _unauthorized(reason: str, request: Request | None = None, kid: object = None) -> HTTPException:
    kid_text = kid if isinstance(kid, str) and KID_RE.fullmatch(kid) else "-"
    audit.warning("auth_failure reason=%s kid=%s %s", _printable(reason, 60), kid_text, _where(request))
    return HTTPException(status_code=401, detail="Invalid or missing credentials.",
                         headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})


def decode_token(token: str, request: Request | None = None) -> Principal:
    """Verify the token and build the Principal. Raises HTTPException 401 (RuntimeError if the auth setup is broken)."""
    validate_auth_config()  # a second line of defence: with no usable setup nothing validates
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise _unauthorized("malformed", request) from e
    kid = header.get("kid")
    if header.get("alg") not in ALGORITHMS:
        raise _unauthorized("alg_not_allowed", request, kid)
    if any(h in header for h in FORBIDDEN_HEADERS):
        raise _unauthorized("key_in_header", request, kid)
    if not isinstance(kid, str) or not KID_RE.fullmatch(kid):
        raise _unauthorized("kid_missing", request)
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as e:
        raise _unauthorized("malformed", request, kid) from e
    issuer = unverified.get("iss")
    realm = realms().get(issuer) if isinstance(issuer, str) else None  # picks the keys to use, nothing more
    if realm is None:
        raise _unauthorized("unknown_issuer", request, kid)
    try:
        key = _cache(realm).key_for(kid)
    except UnknownKid as e:
        raise _unauthorized("unknown_kid", request, kid) from e
    except JwksUnavailable as e:
        log.error("no signing key available for the %s issuer: %s", realm.role, e)
        raise _unauthorized("jwks_unavailable", request, kid) from e
    try:
        claims = jwt.decode(token, key, algorithms=ALGORITHMS, issuer=realm.issuer, audience=realm.audience,
                            leeway=LEEWAY_SECONDS, options={"require": REQUIRED_CLAIMS})
    except jwt.PyJWTError as e:
        raise _unauthorized(type(e).__name__, request, kid) from e
    sub, jti = claims["sub"], claims["jti"]
    if not isinstance(sub, str) or len(sub) > USER_ID_MAX or not re.fullmatch(USER_ID_CHARS, sub):
        raise _unauthorized("invalid_sub", request, kid)
    if not isinstance(jti, str) or not 0 < len(jti) <= 256:
        raise _unauthorized("invalid_jti", request, kid)
    if claims["exp"] - claims["iat"] > get_settings().jwt_max_lifetime_seconds:
        raise _unauthorized("lifetime_too_long", request, kid)
    scope = claims.get("scope", "")  # OAuth style: "chat operator"
    if not isinstance(scope, str):
        raise _unauthorized("invalid_scope", request, kid)
    if "role" in claims and claims["role"] != realm.role:  # the issuer decides the role; a claim may only agree with it
        raise _unauthorized("role_mismatch", request, kid)
    if _denylist.is_revoked(jti):
        raise _unauthorized("revoked", request, kid)
    return Principal(user_id=sub, scopes=frozenset(scope.split()), role=realm.role, jti=jti)


def get_principal(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    if creds is None:
        raise _unauthorized("missing_bearer", request)
    return decode_token(creds.credentials, request)


def _denied(request: Request, principal: Principal, reason: str) -> None:
    audit.warning("auth_denied reason=%s role=%s %s", reason, principal.role, _where(request))


def _require_role_and_scope(role: str, scope: str) -> Callable[..., Principal]:
    def dependency(request: Request, principal: Principal = Depends(get_principal)) -> Principal:
        if principal.role != role:
            _denied(request, principal, "wrong_role")
            raise HTTPException(status_code=403, detail=f"This endpoint is not available for the {principal.role} role.")
        if scope not in principal.scopes:
            _denied(request, principal, "missing_scope")
            raise HTTPException(status_code=403, detail=f"Missing required scope: {scope}.")
        wait = _limiter(role).hit(principal.user_id)  # per authenticated caller, only after the checks above
        if wait:
            _denied(request, principal, "rate_limited")
            raise HTTPException(status_code=429, detail="Too many requests. Try again shortly.",
                                headers={"Retry-After": str(math.ceil(wait))})
        return principal

    return dependency


# merchant endpoints (/chat, /conversations): a merchant with the `chat` scope. An operator token is refused
# even if it also carries the `chat` scope.
require_merchant = _require_role_and_scope(ROLE_MERCHANT, "chat")
# operator endpoints (/operator/*): an operator with the `operator` scope. A merchant token is refused
# even if it carries the `operator` scope.
require_operator = _require_role_and_scope(ROLE_OPERATOR, "operator")
