"""Writes the two demo realms (merchants and operators) next to this file.

Run it only to regenerate them:   python infra/keycloak/gen_realms.py
The realms are separate on purpose: each has its own issuer, its own signing keys (JWKS) and its own audience,
so a merchant token can never be valid for the operator API, whatever its claims say.
Passwords are NOT in the files: Keycloak substitutes ${DEMO_USER_PASSWORD} from its own environment at import.
"""
import json
from pathlib import Path

HERE = Path(__file__).parent


def scope(name: str, in_token: bool = True) -> dict:
    return {"name": name, "protocol": "openid-connect",
            "attributes": {"include.in.token.scope": str(in_token).lower(), "display.on.consent.screen": "false"}}


BASIC = {**scope("basic", in_token=False),
         "protocolMappers": [{"name": "sub", "protocol": "openid-connect", "protocolMapper": "oidc-sub-mapper",
                              "config": {"access.token.claim": "true", "introspection.token.claim": "true"}},
                             {"name": "auth_time", "protocol": "openid-connect", "protocolMapper": "oidc-usersessionmodel-note-mapper",
                              "config": {"user.session.note": "AUTH_TIME", "claim.name": "auth_time", "jsonType.label": "long",
                                         "access.token.claim": "true", "introspection.token.claim": "true"}}]}


def realm(name: str, client_id: str, audience: str, role: str, scope_name: str, users: list[str]) -> dict:
    return {
        "realm": name, "enabled": True, "sslRequired": "external", "registrationAllowed": False,
        "bruteForceProtected": True,
        "accessTokenLifespan": 300,  # 5 minutes: short on purpose (the API also refuses anything above JWT_MAX_LIFETIME_SECONDS)
        "clientScopes": [BASIC, scope(scope_name)],
        "clients": [{
            "clientId": client_id, "enabled": True, "publicClient": True,
            "directAccessGrantsEnabled": True,  # demo only: lets a script ask for a token with user + password
            "standardFlowEnabled": False, "serviceAccountsEnabled": False,
            "defaultClientScopes": ["basic", scope_name], "optionalClientScopes": [],
            "protocolMappers": [
                {"name": "audience", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
                 "config": {"included.custom.audience": audience, "access.token.claim": "true", "id.token.claim": "false"}},
                {"name": "role", "protocol": "openid-connect", "protocolMapper": "oidc-hardcoded-claim-mapper",
                 "config": {"claim.name": "role", "claim.value": role, "jsonType.label": "String",
                            "access.token.claim": "true", "id.token.claim": "false"}},
            ],
        }],
        "users": [{"id": u, "username": u, "enabled": True, "emailVerified": True,
                   "firstName": u, "lastName": "demo", "email": f"{u}@example.invalid",
                   "credentials": [{"type": "password", "value": "${DEMO_USER_PASSWORD}", "temporary": False}]}
                  for u in users],
    }


if __name__ == "__main__":
    (HERE / "realm-merchants.json").write_text(json.dumps(
        realm("getnet-merchants", "getnet-support-app", "getnet-support-api", "merchant", "chat",
              ["cliente1988", "cliente2024", "cliente3030"]), indent=2) + "\n", encoding="utf-8")
    (HERE / "realm-operators.json").write_text(json.dumps(
        realm("getnet-operators", "getnet-operator-app", "getnet-operator-api", "operator", "operator", ["ops1"]),
        indent=2) + "\n", encoding="utf-8")
    print("written")
