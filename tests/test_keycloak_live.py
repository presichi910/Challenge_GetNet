"""The auth code against a REAL Keycloak (infra/keycloak realms). Skipped unless you ask for it:

    docker compose up -d keycloak          (needs KEYCLOAK_ADMIN_PASSWORD and DEMO_USER_PASSWORD in .env)
    set RUN_KEYCLOAK_TESTS=1 & set DEMO_USER_PASSWORD=<the same value> & python -m pytest tests/test_keycloak_live.py -q

CI has no Keycloak, so these never run there; every other auth test uses generated keys and a fake JWKS endpoint.
"""
import base64
import json
import os
import urllib.parse
import urllib.request

import pytest
from fastapi import HTTPException

from app import auth, jwks
from app.auth import decode_token
from app.config import get_settings

BASE = os.environ.get("KEYCLOAK_URL", "http://localhost:8180")
pytestmark = pytest.mark.skipif(os.environ.get("RUN_KEYCLOAK_TESTS") != "1", reason="needs a running Keycloak (RUN_KEYCLOAK_TESTS=1)")


def realm_token(realm: str, client: str, user: str) -> str:
    body = urllib.parse.urlencode({"grant_type": "password", "client_id": client, "username": user,
                                   "password": os.environ["DEMO_USER_PASSWORD"]}).encode()
    with urllib.request.urlopen(urllib.request.Request(f"{BASE}/realms/{realm}/protocol/openid-connect/token", data=body), timeout=20) as r:
        return json.load(r)["access_token"]


@pytest.fixture
def live(monkeypatch):
    for k, v in {"JWKS_URL": f"{BASE}/realms/getnet-merchants/protocol/openid-connect/certs",
                 "JWT_ISSUER": f"{BASE}/realms/getnet-merchants", "JWT_AUDIENCE": "getnet-support-api",
                 "OPERATOR_JWKS_URL": f"{BASE}/realms/getnet-operators/protocol/openid-connect/certs",
                 "OPERATOR_JWT_ISSUER": f"{BASE}/realms/getnet-operators", "OPERATOR_JWT_AUDIENCE": "getnet-operator-api"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    auth.reset_auth_state()
    yield
    get_settings.cache_clear()
    auth.reset_auth_state()


def test_a_merchant_token_from_keycloak_is_accepted_with_the_realm_identity(live):
    p = decode_token(realm_token("getnet-merchants", "getnet-support-app", "cliente1988"))
    assert (p.user_id, p.role, p.scopes) == ("cliente1988", "merchant", frozenset({"chat"})) and p.jti


def test_an_operator_token_from_keycloak_is_an_operator_and_only_that(live):
    p = decode_token(realm_token("getnet-operators", "getnet-operator-app", "ops1"))
    assert (p.user_id, p.role, p.scopes) == ("ops1", "operator", frozenset({"operator"}))


def test_a_tampered_keycloak_token_is_refused(live):
    head, payload, sig = realm_token("getnet-merchants", "getnet-support-app", "cliente1988").split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = "cliente2024"  # try to become another customer
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    with pytest.raises(HTTPException) as e:
        decode_token(f"{head}.{forged}.{sig}")
    assert e.value.status_code == 401


def test_the_keys_are_fetched_once_and_the_encryption_key_is_ignored(live, monkeypatch):
    calls = []
    real = jwks.fetch_jwks
    monkeypatch.setattr(jwks, "fetch_jwks", lambda url, timeout: calls.append(url) or real(url, timeout))
    token = realm_token("getnet-merchants", "getnet-support-app", "cliente3030")
    for _ in range(3):
        assert decode_token(token).user_id == "cliente3030"
    assert len(calls) == 1
