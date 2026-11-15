"""Ask the identity provider (Keycloak, from docker-compose) for a demo token. Development helper.

    python -m scripts.issue_token --user cliente1988                   # a merchant, for /chat
    python -m scripts.issue_token --role operator --operator ops1      # support staff, for /operator/*

It signs nothing: the identity provider does. It needs the demo password in DEMO_USER_PASSWORD (the one compose gives
Keycloak) and the issuers in .env (JWT_ISSUER / OPERATOR_JWT_ISSUER, as the host sees them, e.g. http://localhost:8180/...).
It is NOT copied into the Docker image: nobody who can open a shell in the API container can mint a token.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from app.config import get_settings

CLIENTS = {"merchant": "getnet-support-app", "operator": "getnet-operator-app"}  # public clients of infra/keycloak


def request_token(issuer: str, client_id: str, user: str, password: str, timeout: float = 15.0) -> str:
    """Password grant against the realm of `issuer` (demo only: the realm files enable it)."""
    body = urllib.parse.urlencode({"grant_type": "password", "client_id": client_id, "username": user,
                                   "password": password}).encode()
    req = urllib.request.Request(f"{issuer.rstrip('/')}/protocol/openid-connect/token", data=body)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - the URL comes from our own settings
        return json.load(r)["access_token"]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--role", choices=("merchant", "operator"), default="merchant",
                    help="merchant (default) uses /chat; operator uses /operator/* and can not use /chat")
    ap.add_argument("--user", help="merchant id, the `sub` of the token (e.g. cliente1988). Role merchant only")
    ap.add_argument("--operator", help="operator id, the `sub` of the token (e.g. ops1). Role operator only")
    ap.add_argument("--url", default="http://localhost:8000", help="base URL used in the example curl")
    args = ap.parse_args(argv)
    if args.role == "operator":
        if not args.operator or args.user:
            ap.error("--role operator needs --operator <id> and takes no --user")
        subject = args.operator
    else:
        if not args.user or args.operator:
            ap.error("--role merchant needs --user <id> and takes no --operator")
        subject = args.user

    password = os.environ.get("DEMO_USER_PASSWORD", "")
    if not password:
        ap.error("set DEMO_USER_PASSWORD (the demo password compose gave Keycloak)")
    s = get_settings()
    issuer = s.operator_jwt_issuer if args.role == "operator" else s.jwt_issuer
    if not issuer:
        ap.error("JWT_ISSUER / OPERATOR_JWT_ISSUER are not set (see .env.example)")
    try:
        token = request_token(issuer, CLIENTS[args.role], subject, password)
    except (urllib.error.URLError, KeyError, ValueError) as e:
        raise SystemExit(f"could not get a token from {issuer}: {type(e).__name__}. Is Keycloak up (docker compose up -d keycloak)?")
    print(token)
    print(f"\n# {args.role}={subject}, issued by {issuer}. Try it:\n")
    if args.role == "operator":
        print(f"curl -s \"{args.url}/operator/handoffs?status=pending\" \\\n"
              f"  -H \"Authorization: Bearer {token}\"")
    else:
        print(f"curl -s -X POST {args.url}/chat \\\n"
              f"  -H \"Authorization: Bearer {token}\" \\\n"
              "  -H \"Content-Type: application/json\" \\\n"
              "  -d '{\"message\": \"When will I receive my payout?\"}'")


if __name__ == "__main__":
    main()
