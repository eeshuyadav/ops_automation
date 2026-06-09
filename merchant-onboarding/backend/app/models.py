"""SQLAlchemy models — mirrors ops_infra/3/poller/schema.sql exactly.

If you edit schema.sql, edit this file too.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    mid: Mapped[str] = mapped_column(Text, unique=True, nullable=False)          # A
    merchant_size: Mapped[str | None] = mapped_column(Text)                      # B
    eb_go_live_date: Mapped[str | None] = mapped_column(Text)                    # C
    kyc_spoc: Mapped[str | None] = mapped_column(Text)                           # D
    gokwik_kyc_complete_date: Mapped[str | None] = mapped_column(Text)           # E
    merchant_name: Mapped[str | None] = mapped_column(Text)                      # F
    entity_name: Mapped[str | None] = mapped_column(Text)                        # G
    email: Mapped[str | None] = mapped_column(Text)                              # H
    website: Mapped[str | None] = mapped_column(Text)                            # I
    onboarding: Mapped[str | None] = mapped_column(Text)                         # J
    entity: Mapped[str | None] = mapped_column(Text)                             # K
    name_normalized: Mapped[str | None] = mapped_column(Text, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EasebuzzOnboarding(Base):
    __tablename__ = "easebuzz_onboarding"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="SET NULL"),
    )
    merchant_name: Mapped[str] = mapped_column(Text, nullable=False)
    name_normalized: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    merchant_size: Mapped[str | None] = mapped_column(Text)
    onboarding_status: Mapped[str | None] = mapped_column(Text, index=True)
    kickstart_date: Mapped[str | None] = mapped_column(Text)
    kickstart_date_parsed: Mapped[date | None] = mapped_column(Date)
    kickstart_time: Mapped[str | None] = mapped_column(Text)
    docs_received_date: Mapped[str | None] = mapped_column(Text)
    docs_received_time: Mapped[str | None] = mapped_column(Text)
    days_taken_ks_to_ds: Mapped[str | None] = mapped_column(Text)
    time_taken_ks_to_ds: Mapped[str | None] = mapped_column(Text)
    kyc_completed_by_ops: Mapped[str | None] = mapped_column(Text)
    days_taken_kyc: Mapped[str | None] = mapped_column(Text)
    date_email_sent_to_eb: Mapped[str | None] = mapped_column(Text)
    salt_key_receipt: Mapped[str | None] = mapped_column(Text)
    time_taken_by_eb: Mapped[str | None] = mapped_column(Text)
    salt_key_from_docs_recd: Mapped[str | None] = mapped_column(Text)
    salt_key_from_kickstart: Mapped[str | None] = mapped_column(Text)
    reasons_for_delay_in_eb: Mapped[str | None] = mapped_column(Text)
    promise: Mapped[str | None] = mapped_column(Text)
    delivery: Mapped[str | None] = mapped_column(Text)
    remarks: Mapped[str | None] = mapped_column(Text)
    delay_at_gk: Mapped[str | None] = mapped_column(Text)
    delay_by_merchant: Mapped[str | None] = mapped_column(Text)
    ops_remarks: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, default="sheet")
    last_edited_in_dashboard_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# Note: the `sync_runs` table still exists in Postgres — the poller writes to
# it as a raw-SQL audit log. No ORM mapping is needed here because nothing in
# the API reads it; query it directly via psql if you need the history.


class User(Base):
    """Dashboard login account. Created by an admin via scripts/add_user.py.
    Self-signup is intentionally disabled — only emails the admin has
    pre-provisioned can log in. Domain is also gated at the admin script
    (must match config.allowed_email_domain, default '@gokwik.co').
    """
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False, index=True)
    # bcrypt hash — 60 chars. We never store the plaintext.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
