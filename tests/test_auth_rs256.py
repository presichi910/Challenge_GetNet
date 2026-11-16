"""RS256 + JWKS: what the API accepts from the identity provider and what it refuses.

No network and no real keys: tests/conftest.py generates RSA keys and a FakeIdp serves their public halves.
"""
import logging

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import auth
from app.auth import decode_token
from app.config import get_settings
from app.jwks import JwksCache, JwksUnavailable, UnknownKid
from app.main import app
from app.ratelimit import RateLimiter
from app.revocation import Denylist
from app.schemas import ChatResponse, Routing
from tests.conftest import MERCHANT_JWKS_URL, bearer, jwk_of, make_jwt, operator_jwt, rsa_key


class RecordingService:
    def __init__(self):
        self.calls = 0

    def handle(self, message, user_id, conversation_id=None):
        self.calls += 1
        return ChatResponse(response="ok", user_id=user_id, agents_used=[], routing=Routing(plan=[]), latency_ms=1, trace_id="t")


@pytest.fixture
def svc():
    return RecordingService()


@pytest.fixture
def client(auth_env, svc):
    app.state.service = svc
    with TestClient(app) as c:
        yield c
    app.state.service = None


def chat(client, token):
    return client.post("/chat", json={"message": "hi"}, headers=bearer(token) if token else {})


def revoke(client, jti, token=None):
    return client.post("/operator/revocations", json={"jti": jti}, headers=bearer(token or operator_jwt()))


def reload_settings(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    get_settings.cache_clear()
    auth.reset_auth_state()


# ---------- the token must say which key it was signed with, and may not choose the key ----------

def test_a_valid_rs256_token_is_accepted(auth_env):
    p = decode_token(make_jwt("cliente2024"))
    assert (p.user_id, p.role, p.scopes) == ("cliente2024", "merchant", frozenset({"chat"})) and p.jti


def test_a_token_without_kid_is_401(client, svc):
    assert chat(client, make_jwt(drop_headers=("kid",))).status_code == 401
    assert chat(client, make_jwt(kid="")).status_code == 401
    assert chat(client, make_jwt(kid="a" * 200)).status_code == 401
    assert svc.calls == 0


@pytest.mark.parametrize("header", [{"jku": "https://evil.example/jwks"}, {"jwk": {"kty": "RSA", "n": "AQAB", "e": "AQAB"}},
                                    {"x5u": "https://evil.example/cert.pem"}, {"x5c": ["MIIB"]}])
def test_a_token_that_points_to_its_own_key_is_401(client, svc, header):
    assert chat(client, make_jwt(headers=header)).status_code == 401
    assert svc.calls == 0


def test_a_token_signed_by_an_unknown_key_with_a_known_kid_is_401(client, svc):
    assert chat(client, make_jwt(key=rsa_key("other"))).status_code == 401  # the kid of the real key, the signature of an attacker
    assert chat(client, make_jwt(key=rsa_key("other"), kid="attacker-key")).status_code == 401
    assert svc.calls == 0


def test_the_issuer_picks_the_keys_it_does_not_vouch_for_the_token(client, svc):
    """A merchant-signed token that CLAIMS the operator issuer is checked against the operator keys, and fails."""
    forged = make_jwt("ops1", "operator", iss="https://idp.test/realms/operators", aud="getnet-operator-api")  # merchant key
    r = client.post("/operator/revocations", json={"jti": "x"}, headers=bearer(forged))
    assert r.status_code == 401
    forged_back = operator_jwt("cliente1988", "chat", iss="https://idp.test/realms/merchants", aud="getnet-support-api")
    assert chat(client, forged_back).status_code == 401  # operator-signed, merchant issuer claimed: the merchant keys refuse it
    assert svc.calls == 0


def test_merchant_and_operator_tokens_do_not_cross(client, svc):
    assert chat(client, operator_jwt(scope="chat operator")).status_code == 403  # a real operator token, wrong API
    assert revoke(client, "x", token=make_jwt("cliente1988", "chat operator")).status_code == 403  # a real merchant token
    assert chat(client, make_jwt(aud="getnet-operator-api")).status_code == 401  # the other API's audience on a merchant token
    assert svc.calls == 0


# ---------- the JWKS cache: refresh is bounded, a rotated key works, an IdP outage does not log everybody out ----------

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def cache(auth_env, clock, **kw):
    return JwksCache(MERCHANT_JWKS_URL, clock=clock, **kw)


def test_unknown_kids_trigger_at_most_one_refresh_per_interval(auth_env):
    clock = Clock()
    c = cache(auth_env, clock, ttl=600, min_refresh=30)
    c.key_for("merchant-key-1")
    assert len(auth_env.fetches) == 1
    for i in range(50):  # an attacker sending random kids
        with pytest.raises(UnknownKid):
            c.key_for(f"random-{i}")
    assert len(auth_env.fetches) == 1  # the IdP was not asked again
    clock.now += 31
    with pytest.raises(UnknownKid):
        c.key_for("random-again")
    assert len(auth_env.fetches) == 2  # one refresh once the interval has passed
    for i in range(50):
        with pytest.raises(UnknownKid):
            c.key_for(f"more-{i}")
    assert len(auth_env.fetches) == 2


def test_a_rotated_key_is_found_after_the_allowed_refresh_and_the_old_one_stops_working(auth_env):
    clock = Clock()
    c = cache(auth_env, clock, ttl=600, min_refresh=30)
    c.key_for("merchant-key-1")
    auth_env.published[MERCHANT_JWKS_URL] = [("merchant-key-2", "rotated")]  # the IdP rotates its signing key
    with pytest.raises(UnknownKid):
        c.key_for("merchant-key-2")  # too soon to look again
    clock.now += 31
    assert c.key_for("merchant-key-2") is not None
    with pytest.raises(UnknownKid):
        c.key_for("merchant-key-1")  # the retired key is gone


def test_an_outage_of_the_idp_keeps_serving_the_cached_keys(auth_env):
    clock = Clock()
    c = cache(auth_env, clock, ttl=600, min_refresh=30)
    c.key_for("merchant-key-1")
    auth_env.down = True
    clock.now += 5000  # far past the ttl
    assert c.key_for("merchant-key-1") is not None  # fail static: customers stay logged in
    with pytest.raises(UnknownKid):
        c.key_for("never-published")


def test_a_cache_that_never_got_a_key_fails_closed(auth_env, client, svc):
    auth_env.down = True
    with pytest.raises(JwksUnavailable):
        cache(auth_env, Clock()).key_for("merchant-key-1")
    assert chat(client, make_jwt()).status_code == 401 and svc.calls == 0


def test_only_rs256_signing_keys_with_a_kid_are_kept(auth_env):
    enc = jwk_of("merchants", "enc-key", use="enc", alg="RSA-OAEP")  # Keycloak publishes one of these too
    no_kid = {k: v for k, v in jwk_of("other", "x").items() if k != "kid"}
    other_alg = jwk_of("other", "ps-key", alg="PS256")
    auth_env.extra[MERCHANT_JWKS_URL] = [enc, no_kid, other_alg, {"kty": "oct", "kid": "sym", "k": "AAAA"}, "junk"]
    c = cache(auth_env, Clock())
    assert c.key_for("merchant-key-1") is not None
    for kid in ("enc-key", "ps-key", "sym"):
        with pytest.raises(UnknownKid):
            c.key_for(kid)


def test_the_api_follows_a_rotation_through_the_bounded_refresh(auth_env, monkeypatch):
    reload_settings(monkeypatch, JWKS_MIN_REFRESH_SECONDS=0)  # allow a refresh on every unknown kid
    decode_token(make_jwt())
    auth_env.published[MERCHANT_JWKS_URL] = [("merchant-key-2", "rotated")]
    assert decode_token(make_jwt(key=rsa_key("rotated"), kid="merchant-key-2")).user_id == "cliente1988"
    with pytest.raises(HTTPException):
        decode_token(make_jwt())  # signed with the retired key


# ---------- lifetime ----------

def test_a_token_may_live_up_to_the_maximum_and_not_a_second_more(auth_env):
    assert decode_token(make_jwt(minutes=15)).user_id == "cliente1988"  # 900 s = JWT_MAX_LIFETIME_SECONDS
    with pytest.raises(HTTPException) as e:
        decode_token(make_jwt(minutes=15.02))  # 901 s
    assert e.value.status_code == 401


# ---------- revocation ----------

def test_an_operator_can_revoke_a_token_before_it_expires(client, svc):
    token = make_jwt("cliente1988", jti="stolen-laptop-token")
    other = make_jwt("cliente1988", jti="another-token")
    assert chat(client, token).status_code == 200
    assert revoke(client, "stolen-laptop-token").status_code == 204
    assert chat(client, token).status_code == 401  # the next request is refused
    assert chat(client, other).status_code == 200  # nothing else changed


def test_only_an_operator_can_revoke_and_the_body_is_checked(client):
    assert revoke(client, "x", token=make_jwt("cliente1988")).status_code == 403
    assert client.post("/operator/revocations", json={"jti": "x"}).status_code == 401
    assert client.post("/operator/revocations", json={"jti": ""}, headers=bearer(operator_jwt())).status_code == 422
    assert client.post("/operator/revocations", json={"jti": "bad id\nline"}, headers=bearer(operator_jwt())).status_code == 422


def test_a_revocation_lasts_as_long_as_a_token_could():
    clock = Clock()
    d = Denylist(clock=clock)
    d.revoke("a", ttl_seconds=905)
    assert d.is_revoked("a") and not d.is_revoked("b")
    clock.now += 906
    assert not d.is_revoked("a")  # the token is dead by then anyway; the entry is dropped


# ---------- rate limit per sub ----------

def test_a_caller_over_the_limit_gets_429_with_retry_after_and_others_are_not_affected(client, svc, monkeypatch):
    reload_settings(monkeypatch, RATE_LIMIT_PER_MINUTE=3)
    a, b = lambda: make_jwt("cliente1988"), lambda: make_jwt("cliente2024")
    assert [chat(client, a()).status_code for _ in range(3)] == [200, 200, 200]
    r = chat(client, a())
    assert r.status_code == 429 and 1 <= int(r.headers["retry-after"]) <= 60
    assert svc.calls == 3  # the refused request never reached the orchestrator (no model quota spent)
    assert chat(client, b()).status_code == 200  # another customer has their own allowance


def test_operators_have_their_own_limit_and_unauthenticated_requests_use_none(client, monkeypatch):
    reload_settings(monkeypatch, OPERATOR_RATE_LIMIT_PER_MINUTE=2, RATE_LIMIT_PER_MINUTE=1)
    assert [revoke(client, f"j{i}").status_code for i in range(3)] == [204, 204, 429]
    assert chat(client, make_jwt("cliente1988")).status_code == 200  # the merchant allowance is separate
    for _ in range(5):
        assert chat(client, None).status_code == 401  # no token: refused before it counts
        assert chat(client, "garbage").status_code == 401
    assert chat(client, make_jwt("cliente2024")).status_code == 200


def test_a_limit_of_zero_means_no_limit(client, monkeypatch):
    reload_settings(monkeypatch, RATE_LIMIT_PER_MINUTE=0)
    assert all(chat(client, make_jwt()).status_code == 200 for _ in range(40))


def test_the_window_slides_and_a_refused_request_does_not_count():
    clock = Clock()
    rl = RateLimiter(2, window=60, clock=clock)
    assert rl.hit("a") == 0 and rl.hit("a") == 0
    assert 0 < rl.hit("a") <= 60
    clock.now += 61
    assert rl.hit("a") == 0 and rl.hit("a") == 0  # the first two aged out; the refused one was never counted
    assert rl.hit("a") > 0 and rl.hit("b") == 0


# ---------- audit log: why a request was refused, never the token ----------

def test_a_refusal_is_logged_with_a_reason_and_without_the_token_or_the_claims(client, caplog):
    token = make_jwt("cliente1988", key=rsa_key("other"))
    with caplog.at_level(logging.WARNING, logger="auth.audit"):
        assert chat(client, token).status_code == 401
        assert chat(client, make_jwt(minutes=-1)).status_code == 401
        assert chat(client, None).status_code == 401
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "auth.audit")
    assert "reason=InvalidSignatureError" in text and "reason=ExpiredSignatureError" in text and "reason=missing_bearer" in text
    assert "path=/chat" in text and "method=POST" in text and "client=" in text
    assert token not in text and token.split(".")[1] not in text and "cliente1988" not in text  # no token, no claims


def test_what_the_caller_writes_cannot_forge_a_log_line(client, caplog):
    evil_kid = "good\nauth_failure reason=fake kid=x"
    with caplog.at_level(logging.WARNING, logger="auth.audit"):
        assert chat(client, make_jwt(kid=evil_kid)).status_code == 401
        assert client.get("/conversations/c%0Aauth_failure%20reason=fake").status_code == 401  # a newline inside the path
    lines = [r.getMessage() for r in caplog.records if r.name == "auth.audit"]
    assert len(lines) == 2 and all("\n" not in l and "\r" not in l for l in lines)
    # one line per event, and each one starts with its REAL reason: what the caller wrote stays inside its own field
    assert lines[0].startswith("auth_failure reason=kid_missing kid=- ")
    assert lines[1].startswith("auth_failure reason=missing_bearer kid=- method=GET path=/conversations/")


def test_a_403_and_a_429_are_logged_too(client, caplog, monkeypatch):
    reload_settings(monkeypatch, RATE_LIMIT_PER_MINUTE=1)
    with caplog.at_level(logging.WARNING, logger="auth.audit"):
        chat(client, operator_jwt(scope="chat"))  # wrong role
        chat(client, make_jwt(scope="other"))  # wrong scope
        chat(client, make_jwt("cliente2024"))
        chat(client, make_jwt("cliente2024"))  # over the limit
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "auth.audit")
    assert "reason=wrong_role" in text and "reason=missing_scope" in text and "reason=rate_limited" in text


# ---------- revocations survive a restart (SQLite) and are pruned by expiry ----------

def test_a_revocation_survives_a_restart_of_the_api(client, svc, tmp_path):
    db = str(tmp_path / "revocations.sqlite")
    assert auth.open_persistent_denylist(db) == 0
    token = make_jwt("cliente1988", jti="stolen-laptop-token")
    assert chat(client, token).status_code == 200
    assert revoke(client, "stolen-laptop-token").status_code == 204
    assert chat(client, token).status_code == 401
    auth.reset_auth_state()  # the process stops: key caches, limiters and the in-memory state are gone
    assert chat(client, token).status_code == 200  # a fresh, EMPTY denylist would let the token back in...
    assert auth.open_persistent_denylist(db) == 1  # ...but the API opens the file at startup and finds the entry
    assert chat(client, token).status_code == 401
    assert chat(client, make_jwt("cliente1988", jti="another-token")).status_code == 200


def test_the_revocations_file_holds_only_the_jti_and_when_it_expires(tmp_path):
    db = tmp_path / "revocations.sqlite"
    token = make_jwt("cliente1988", jti="the-jti")
    d = Denylist(db)
    d.revoke("the-jti", ttl_seconds=900)
    d.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.glob("revocations.sqlite*"))
    assert b"the-jti" in raw and token.encode() not in raw and b"cliente1988" not in raw


def test_expired_revocations_are_pruned_when_the_file_is_opened_and_on_every_new_revocation(tmp_path):
    clock = Clock()
    db = tmp_path / "revocations.sqlite"
    d = Denylist(db, clock=clock)
    d.revoke("old", ttl_seconds=100)
    d.revoke("alive", ttl_seconds=5000)
    assert d.count() == 2
    clock.now += 200  # "old" expires
    assert not d.is_revoked("old") and d.is_revoked("alive")
    d.close()
    reopened = Denylist(db, clock=clock)  # opening prunes
    assert reopened.count() == 1 and reopened.is_revoked("alive")
    clock.now += 6000
    reopened.revoke("new", ttl_seconds=100)  # a new revocation prunes too
    assert reopened.count() == 1 and reopened.is_revoked("new") and not reopened.is_revoked("alive")


def test_the_api_opens_the_revocations_file_at_startup_and_closes_it_at_shutdown():
    import inspect

    from app import main

    source = inspect.getsource(main.lifespan)
    assert "open_persistent_denylist(s.revocations_db_path)" in source and "reset_auth_state()" in source
