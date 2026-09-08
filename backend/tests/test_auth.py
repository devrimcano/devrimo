import time

import jwt as pyjwt

from app.auth import dependencies as auth_dependencies
from app.auth import jwt as auth_jwt
from app.config import get_settings
from tests.conftest import new_user_id


async def test_expired_token_is_401(client):
    token = pyjwt.encode(
        {"sub": str(new_user_id()), "aud": "authenticated", "exp": int(time.time()) - 10},
        "test-secret-not-for-production",
        algorithm="HS256",
    )
    response = await client.get("/api/v1/agents/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_wrong_signature_is_401(client):
    token = pyjwt.encode(
        {"sub": str(new_user_id()), "aud": "authenticated", "exp": int(time.time()) + 3600},
        "not-the-real-secret",
        algorithm="HS256",
    )
    response = await client.get("/api/v1/agents/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


async def test_garbage_bearer_is_401(client):
    response = await client.get("/api/v1/agents/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


async def test_authenticated_request_verifies_its_jwt_once(client, monkeypatch):
    calls = 0
    original = auth_jwt.verify_access_token

    def counted(token: str):
        nonlocal calls
        calls += 1
        return original(token)

    monkeypatch.setattr(auth_jwt, "verify_access_token", counted)
    monkeypatch.setattr(auth_dependencies, "verify_access_token", counted)

    response = await client.get(
        "/api/v1/profile",
        headers={
            "Authorization": (
                "Bearer "
                + pyjwt.encode(
                    {
                        "sub": str(new_user_id()),
                        "aud": "authenticated",
                        "iss": get_settings().jwt_issuer,
                        "exp": int(time.time()) + 3600,
                    },
                    "test-secret-not-for-production",
                    algorithm="HS256",
                )
            )
        },
    )

    assert response.status_code == 200
    assert calls == 1
