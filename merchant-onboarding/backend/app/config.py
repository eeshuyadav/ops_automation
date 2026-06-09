from datetime import date

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = (
        "postgresql+asyncpg://onb:onb@localhost:5433/merchant_onboarding"
    )
    frontend_url: str = "http://localhost:5173"
    poller_dir: str = "../poller"
    app_env: str = "development"

    # JWT auth — replaces the old X-API-Key model. Each user logs in via
    # /api/auth/login and receives a bearer token signed with this secret.
    # MUST be set to a long random string in production; main.py refuses to
    # start if it's empty and app_env is not "development".
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_hours: int = 12  # token lifetime; users re-login after this

    # Email domain allow-list for login. Only addresses ending in one of
    # these suffixes are accepted by the admin add_user script AND by the
    # /api/auth/login endpoint (defense in depth). Comma-separated env var.
    allowed_email_domains: str = "@gokwik.co"

    # Specific email allow-list for sign-in. Listed emails are accepted
    # by /api/auth/google after the Google ID-token's domain check
    # already passes. Format: comma-separated lowercase emails. Empty
    # string means "any email in allowed_email_domains is OK" — the
    # default for a Workspace-restricted OAuth client.
    allowed_emails: str = ""

    # Google OAuth client ID — the public identifier of the OAuth 2.0
    # Web Application client in the Gokwik Google Cloud project. The
    # frontend uses this to render the Sign-In With Google button; the
    # backend uses it to verify the audience claim of incoming ID tokens.
    # MUST be set in production. Empty disables Google sign-in entirely.
    google_client_id: str = ""

    # X-API-Key — kept as an OPTIONAL secondary auth path for the poller's
    # /api callers (it has none today, but leaving the hook in place is
    # cheap). Empty string disables it.
    api_key: str = ""

    # Submerchant seeding cutoff: rows whose Gokwik KYC complete date is on
    # or before this date are assumed to already live in the one-time
    # Easebuzz backfill and are not seeded. Configurable via env var
    # KYC_SEED_CUTOFF (ISO date, e.g. "2026-05-05").
    kyc_seed_cutoff: date = date(2026, 5, 5)

    # Slack reader — pulls salt & key receipt dates from one channel.
    # Empty token disables the Slack sync step in the poller (poller still
    # runs the sheet sync, just logs that Slack was skipped).
    slack_bot_token: str = ""
    slack_salt_key_channel_id: str = ""
    slack_lookback_days: int = 14

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def is_dev(self) -> bool:
        return self.app_env == "development"

    @property
    def allowed_email_domains_list(self) -> list[str]:
        """Parsed comma-separated form, normalized to lowercase and stripped.
        Always returns at least the default if env is malformed."""
        raw = (self.allowed_email_domains or "@gokwik.co").strip()
        out = [d.strip().lower() for d in raw.split(",") if d.strip()]
        return out or ["@gokwik.co"]

    @property
    def allowed_emails_set(self) -> set[str]:
        """Parsed self-signup allow-list as a lowercased set for O(1) lookup."""
        raw = (self.allowed_emails or "").strip()
        return {e.strip().lower() for e in raw.split(",") if e.strip()}


settings = Settings()
