"""Shared FastAPI dependencies.

Keep this module dependency-light — it must NOT import from app.routers
or anything that would re-enter the router graph. It DOES import from
app.db / app.models because the JWT path needs to look up the user.
"""
from __future__ import annotations

import hmac
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import settings
from app.db import get_db


# Password helpers removed — auth pivoted to Google Sign-In. The
# `password_hash` column on User stays for forward compatibility but
# is never read or written now. If a future flow needs passwords again,
# import `bcrypt` directly (skip passlib which fights bcrypt 4.x's API).


# Synthetic user returned by the dev-mode auth bypass (see `require_user`).
# Built once at module import; SQLAlchemy isn't attached so it behaves
# like a frozen dataclass for the handlers that just read .email / .id.
_DEV_USER = models.User(
    id=uuid.UUID("00000000-0000-0000-0000-0000000000de"),
    email="dev@localhost",
    password_hash="",
    is_active=True,
    created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    last_login_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
)


def _dev_bypass_active() -> bool:
    """True when auth is intentionally OFF — happens when GOOGLE_CLIENT_ID
    is unset AND we're in development. Lets the dashboard be usable while
    the OAuth client is being provisioned in Google Cloud Console.
    Production main.py refuses to boot with empty client_id so this can
    never fire in prod (see settings.is_dev guard).
    """
    return settings.is_dev and not settings.google_client_id.strip()


def issue_jwt(user_id: uuid.UUID, email: str) -> str:
    """Sign a short-lived JWT for the logged-in user."""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "iat": now,
        "exp": now + timedelta(hours=settings.jwt_expire_hours),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_jwt(token: str) -> dict[str, Any]:
    """Verify signature + expiry. Raises jwt.PyJWTError subclass on bad token."""
    return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])


# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------

async def require_user(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> models.User:
    """FastAPI dependency that enforces a valid JWT and returns the user.

    Reads the `Authorization: Bearer <token>` header. Verifies the token's
    signature + expiry against `settings.jwt_secret_key`, looks up the
    user row, and ensures it's still active. Returns the loaded `User` for
    handlers that want it, or raises 401 if anything is off.

    Dev bypass: when GOOGLE_CLIENT_ID is unset AND app_env=development,
    this function returns a synthetic dev user instead of enforcing
    auth. Used while the OAuth client is being provisioned — set the
    Client ID in .env to flip back to real auth.
    """
    if _dev_bypass_active():
        return _DEV_USER

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header. Expected: 'Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[len("Bearer "):].strip()
    try:
        payload = decode_jwt(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired — please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="Token missing sub claim")
    try:
        user_id = uuid.UUID(sub)
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Token sub is not a UUID")

    user = (
        await db.execute(select(models.User).where(models.User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User no longer exists.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled.",
        )
    return user


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Legacy X-API-Key dependency. Left in place for any script callers
    that still rely on it. Disabled when settings.api_key is empty.
    """
    expected = settings.api_key
    if not expected:
        return
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )


def is_allowed_email(email: str) -> bool:
    """True if the email ends in one of the configured allowed domains."""
    e = (email or "").strip().lower()
    return any(e.endswith(d) for d in settings.allowed_email_domains_list)
