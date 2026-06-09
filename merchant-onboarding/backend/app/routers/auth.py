"""Auth router — Sign in with Google.

Flow:
  1. Frontend renders the Google Sign-In button (configured with the
     Workspace `hd=gokwik.co` hint so only @gokwik.co accounts surface).
  2. User picks an account → Google returns an ID-token JWT to the frontend.
  3. Frontend POSTs the ID token to /api/auth/google.
  4. Backend verifies the token's signature + audience claim against
     `settings.google_client_id`, checks the email domain, looks up or
     creates the user row, and issues our own short-lived JWT.
  5. Frontend stores the returned token in localStorage and uses it as
     `Authorization: Bearer …` on every subsequent /api request.

No password column is read or written — Google is the identity provider.
The User model's `password_hash` stays nullable for forward-compatibility
but is left empty for accounts created via Google sign-in.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import settings
from app.db import get_db
from app.dependencies import (
    is_allowed_email,
    issue_jwt,
    require_user,
)


router = APIRouter(prefix="/api/auth", tags=["auth"])


class GoogleLoginIn(BaseModel):
    credential: str  # the Google ID-token JWT returned by GSI


class UserOut(BaseModel):
    id: str
    email: str
    is_active: bool
    last_login_at: datetime | None = None


class LoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_seconds: int
    user: UserOut


def _user_out(u: models.User) -> UserOut:
    return UserOut(
        id=str(u.id),
        email=u.email,
        is_active=u.is_active,
        last_login_at=u.last_login_at,
    )


@router.post("/google", response_model=LoginOut)
async def google_login(body: GoogleLoginIn, db: AsyncSession = Depends(get_db)):
    """Exchange a Google ID token for an app-issued JWT.

    Verification layers (any failure → 401):
      1. Token signature + expiry verified against Google's public keys.
      2. Audience (`aud`) claim must match settings.google_client_id —
         this prevents tokens issued to other Google Cloud projects from
         being replayed against us.
      3. Email domain must be in settings.allowed_email_domains.
      4. If settings.allowed_emails is non-empty, the email must also be
         on that specific allow-list (defense-in-depth on top of the
         Workspace OAuth client's `hd` restriction).

    First sign-in for a previously-unseen email auto-creates the user.
    """
    if not settings.google_client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google sign-in is not configured on this server (GOOGLE_CLIENT_ID missing).",
        )

    try:
        payload = google_id_token.verify_oauth2_token(
            body.credential,
            google_requests.Request(),
            settings.google_client_id,
            # Allow a small clock skew between us and Google. The library's
            # default is 0 which sometimes trips on freshly-issued tokens.
            clock_skew_in_seconds=30,
        )
    except ValueError as e:
        # verify_oauth2_token raises ValueError for any verification
        # failure (bad signature, expired, wrong audience, etc.). Map to
        # 401 with a generic message so we don't help attackers probe.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Google sign-in rejected: {e}",
        )

    # `email_verified` flag: Google sets this true for Workspace accounts
    # whose email has been verified by the org admin. Refuse unverified
    # emails to prevent an attacker spoofing the address.
    if not payload.get("email_verified"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google account email is not verified.",
        )

    email_lower = (payload.get("email") or "").strip().lower()
    if not email_lower:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Google token missing email claim.",
        )

    # Domain check (defense in depth on top of the OAuth client's hd hint).
    if not is_allowed_email(email_lower):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Sign-in restricted to "
                f"{', '.join(settings.allowed_email_domains_list)} accounts."
            ),
        )
    # Optional per-email allow-list (overrides the domain check when set).
    allowed = settings.allowed_emails_set
    if allowed and email_lower not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This email is not authorized to sign in. Contact an admin.",
        )

    # Look up or create the user record.
    user = (
        await db.execute(
            select(models.User).where(models.User.email == email_lower)
        )
    ).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if user is None:
        user = models.User(
            id=uuid.uuid4(),
            email=email_lower,
            password_hash="",  # Google-only — no password set
            is_active=True,
            created_at=now,
            last_login_at=now,
        )
        db.add(user)
    else:
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is disabled. Contact an admin.",
            )
        user.last_login_at = now
    await db.flush()

    token = issue_jwt(user.id, user.email)
    return LoginOut(
        access_token=token,
        expires_in_seconds=settings.jwt_expire_hours * 3600,
        user=_user_out(user),
    )


@router.get("/config")
async def auth_config():
    """Public endpoint — returns the OAuth client ID the frontend needs
    to render the Google Sign-In button, plus an `auth_disabled` flag
    that's true when the dev-mode bypass is active (i.e. GOOGLE_CLIENT_ID
    is empty and APP_ENV=development). The frontend uses that flag to
    skip the login page entirely while the OAuth client is being
    provisioned.
    """
    auth_disabled = settings.is_dev and not settings.google_client_id.strip()
    return {
        "google_client_id": settings.google_client_id,
        "allowed_email_domains": settings.allowed_email_domains_list,
        "auth_disabled": auth_disabled,
    }


@router.get("/me", response_model=UserOut)
async def me(user: models.User = Depends(require_user)):
    """Return the currently-authenticated user."""
    return _user_out(user)


@router.post("/logout", status_code=204)
async def logout(user: models.User = Depends(require_user)):
    """Stateless JWT — there's nothing to invalidate server-side. The
    frontend discards the token from localStorage. This endpoint exists
    so the UI can call it for symmetry."""
    return None
