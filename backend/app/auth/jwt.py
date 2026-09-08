"""Verification of Supabase-issued access tokens.

Supabase projects sign JWTs one of two ways:

* Asymmetric (ES256/RS256) signing keys — the default for new projects.
  Verified against the project's JWKS endpoint, cached and auto-refreshed
  by PyJWT's ``PyJWKClient``.
* The legacy shared HS256 secret ("JWT secret" in project settings) — kept
  as a fallback for older projects that have not rotated to the new keys.
"""

from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID

import jwt
from fastapi import HTTPException, status
from jwt import PyJWKClient

from app.config import get_settings


@dataclass(frozen=True)
class AuthenticatedUser:
    id: UUID
    email: str | None
    access_token: str


@lru_cache
def _jwks_client() -> PyJWKClient | None:
    settings = get_settings()
    if not settings.supabase_url:
        return None
    return PyJWKClient(settings.jwks_url, cache_keys=True)


def _decode(token: str) -> dict:
    settings = get_settings()
    unverified = jwt.get_unverified_header(token)
    algorithm = unverified.get("alg", "")
    if not isinstance(algorithm, str):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unsupported token algorithm")

    if not settings.supabase_url:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Auth is not configured")
    options = {"require": ["exp", "sub", "iss"]}

    if algorithm == "HS256":
        if not settings.supabase_jwt_secret:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Auth is not configured")
        return jwt.decode(
            token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
            issuer=settings.jwt_issuer,
            options=options,
        )

    if algorithm not in {"ES256", "RS256"}:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unsupported token algorithm")
    client = _jwks_client()
    if client is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Auth is not configured")

    signing_key = client.get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        algorithms=["ES256", "RS256"],
        audience="authenticated",
        issuer=settings.jwt_issuer,
        options=options,
    )


def verify_access_token(token: str) -> AuthenticatedUser:
    try:
        claims = _decode(token)
    except HTTPException:
        raise
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token") from exc

    try:
        user_id = UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token missing a valid subject") from exc

    return AuthenticatedUser(id=user_id, email=claims.get("email"), access_token=token)
