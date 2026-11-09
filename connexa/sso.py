"""Console SSO — IdP-initiated signed assertion login.

The platform's IdP signs a compact JWS (HS256, the "JWT bearer" pattern) with a
shared secret; Connexa verifies signature, expiry, nbf and issuer, then mints
the same admin session cookie as a token login. Standard begins-here support:
works behind any IdP that can post a signed JWT through a browser or script
(form POST /dashboard/sso/login with id_token=...).

Value is symmetric-authenticated: treat CONNEXA_SSO_SECRET like a password and
scope the issuer so only your IdP's tokens are honored.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def sign(payload: dict, secret: str) -> str:
    """Mint an assertion (HS256 JWS). Provided so self-hosters can script logins
    the same way an IdP would — and used by the tests."""
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(payload).encode())
    sig = _b64(hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"


def verify(token: str, secret: str, issuer: str = "") -> dict:
    """Verify signature, expiry, nbf and issuer. Raises ValueError on any failure."""
    try:
        header_b, body_b, sig_b = token.strip().split(".")
        expected = hmac.new(secret.encode(), f"{header_b}.{body_b}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(sig_b)):
            raise ValueError("bad signature")
        payload = json.loads(_unb64(body_b))
    except ValueError:
        raise
    except Exception as error:  # noqa: BLE001 — any decode problem is an auth failure
        raise ValueError(f"malformed assertion: {error}")
    now = time.time()
    if payload.get("exp") and payload["exp"] < now - 30:
        raise ValueError("assertion expired")
    if payload.get("nbf") and payload["nbf"] > now + 30:
        raise ValueError("assertion not yet valid")
    if issuer and payload.get("iss") != issuer:
        raise ValueError("unexpected issuer")
    return payload


if __name__ == "__main__":   # mint an assertion the same way an IdP would
    import argparse
    import os

    parser = argparse.ArgumentParser(description="Mint an SSO assertion for the Connexa console")
    parser.add_argument("--email", required=True)
    parser.add_argument("--secret", default=os.getenv("CONNEXA_SSO_SECRET", ""))
    parser.add_argument("--issuer", default=os.getenv("CONNEXA_SSO_ISSUER", ""))
    parser.add_argument("--ttl", type=int, default=300)
    args = parser.parse_args()
    if not args.secret:
        raise SystemExit("Set CONNEXA_SSO_SECRET (or pass --secret) first.")
    payload = {"iss": args.issuer, "email": args.email, "sub": args.email,
               "iat": int(time.time()), "exp": int(time.time()) + args.ttl}
    print(sign(payload, args.secret))
