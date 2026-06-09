-- Merchant Onboarding dashboard schema.
-- Source of truth for the local `merchant_onboarding` Postgres.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ---------------------------------------------------------------------------
-- merchants
-- ---------------------------------------------------------------------------
-- One row per MID in the Gokwik Submerchant list ("Merchant Onboarding" tab,
-- spreadsheet 1-Mj_dTa..., gid 335949376). MID is the natural key.
--
-- We import cols A and C..K only — col B ("Signup/KYC Completion date By
-- Gokwik") and everything after col K are intentionally excluded.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS merchants (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    mid                      TEXT NOT NULL UNIQUE,   -- A
    merchant_size            TEXT,                   -- B (col header is stale: actually holds "Emerging"/"Emerging - Custom"/"SME"/...)
    eb_go_live_date          TEXT,                   -- C
    kyc_spoc                 TEXT,                   -- D
    gokwik_kyc_complete_date TEXT,                   -- E
    merchant_name            TEXT,                   -- F
    entity_name              TEXT,                   -- G
    email                    TEXT,                   -- H
    website                  TEXT,                   -- I
    onboarding               TEXT,                   -- J ("New" / "Existing" / ...)
    entity                   TEXT,                   -- K ("PVT LTD" / "Proprietorship" / ...)
    name_normalized          TEXT,                   -- derived from F, for joining
    first_seen_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_synced_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent ALTER for existing DBs.
ALTER TABLE merchants ADD COLUMN IF NOT EXISTS merchant_size TEXT;

CREATE INDEX IF NOT EXISTS merchants_name_normalized_idx
    ON merchants (name_normalized);
CREATE INDEX IF NOT EXISTS merchants_first_seen_idx
    ON merchants (first_seen_at DESC);

-- ---------------------------------------------------------------------------
-- easebuzz_onboarding
-- ---------------------------------------------------------------------------
-- One row per Merchant Name from the "Easebuzz" tab of the Ops Updates sheet
-- (1X5e3r..., gid 0). Source of truth for the onboarding pipeline status.
-- Dashboard edits write here; the sheet itself becomes archival.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS easebuzz_onboarding (
    id                          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    merchant_id                 UUID REFERENCES merchants(id) ON DELETE SET NULL,
    merchant_name               TEXT NOT NULL,
    name_normalized             TEXT NOT NULL UNIQUE,
    merchant_size               TEXT,
    onboarding_status           TEXT,         -- "Live" / "Pending" / "Kickstart" / etc.
    kickstart_date              TEXT,           -- raw text from the sheet
    kickstart_date_parsed       DATE,           -- parsed by poller, for sorting
    kickstart_time              TEXT,
    docs_received_date          TEXT,
    docs_received_time          TEXT,
    days_taken_ks_to_ds         TEXT,
    time_taken_ks_to_ds         TEXT,
    kyc_completed_by_ops        TEXT,
    days_taken_kyc              TEXT,
    date_email_sent_to_eb       TEXT,
    salt_key_receipt            TEXT,
    time_taken_by_eb            TEXT,
    salt_key_from_docs_recd     TEXT,
    salt_key_from_kickstart     TEXT,
    reasons_for_delay_in_eb     TEXT,
    promise                     TEXT,
    delivery                    TEXT,
    remarks                     TEXT,
    delay_at_gk                 TEXT,         -- "Y" / "N"
    delay_by_merchant           TEXT,         -- "Y" / "N"
    ops_remarks                 TEXT,
    -- Provenance + edit tracking
    source                      TEXT NOT NULL DEFAULT 'sheet',  -- 'sheet' | 'dashboard'
    last_edited_in_dashboard_at TIMESTAMPTZ,
    last_synced_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent ALTER for existing DBs: add the parsed-date column if missing.
-- This must run BEFORE the index below since the index references the column.
ALTER TABLE easebuzz_onboarding
    ADD COLUMN IF NOT EXISTS kickstart_date_parsed DATE;

-- Drop the legacy `extra` JSONB column. Never written, never read. Idempotent.
ALTER TABLE easebuzz_onboarding
    DROP COLUMN IF EXISTS extra;

CREATE INDEX IF NOT EXISTS easebuzz_merchant_id_idx
    ON easebuzz_onboarding (merchant_id);
CREATE INDEX IF NOT EXISTS easebuzz_status_idx
    ON easebuzz_onboarding (onboarding_status);
CREATE INDEX IF NOT EXISTS easebuzz_kickstart_parsed_idx
    ON easebuzz_onboarding (kickstart_date_parsed DESC NULLS LAST);
-- Partial index for analytics — every analytics query filters out
-- `source='seeded'` (those rows lack workflow durations), so a partial
-- index on the kickstart sort key is materially smaller than the full
-- index above and lets Postgres use index-only scans for the common case.
CREATE INDEX IF NOT EXISTS easebuzz_source_not_seeded_idx
    ON easebuzz_onboarding (kickstart_date_parsed DESC)
    WHERE source <> 'seeded';

-- ---------------------------------------------------------------------------
-- sync_runs
-- ---------------------------------------------------------------------------
-- Audit log: every poller execution writes one row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_runs (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    started_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at              TIMESTAMPTZ,
    status                   TEXT NOT NULL DEFAULT 'running',  -- running | success | failed
    gokwik_rows_seen         INTEGER NOT NULL DEFAULT 0,
    gokwik_new_merchants     INTEGER NOT NULL DEFAULT 0,
    gokwik_updated_merchants INTEGER NOT NULL DEFAULT 0,
    easebuzz_rows_seen       INTEGER NOT NULL DEFAULT 0,
    easebuzz_new_rows        INTEGER NOT NULL DEFAULT 0,
    easebuzz_updated_rows    INTEGER NOT NULL DEFAULT 0,
    easebuzz_linked_rows     INTEGER NOT NULL DEFAULT 0,
    error                    TEXT,
    triggered_by             TEXT NOT NULL DEFAULT 'cron'      -- cron | api | manual
);

CREATE INDEX IF NOT EXISTS sync_runs_started_idx
    ON sync_runs (started_at DESC);

-- ---------------------------------------------------------------------------
-- users — dashboard login accounts. Auto-created on first successful Google
-- Sign-In by /api/auth/google, which verifies the ID token and enforces the
-- ALLOWED_EMAIL_DOMAINS check before insert. No self-signup endpoint exists.
-- password_hash stays NULL/empty for Google-only accounts.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email           TEXT NOT NULL UNIQUE,
    password_hash   TEXT NOT NULL,                -- bcrypt $2b$… 60-char
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS users_email_idx ON users (LOWER(email));
