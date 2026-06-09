from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models, schemas
from app.adapters import merchant_out
from app.db import get_db

router = APIRouter(prefix="/api/merchants", tags=["merchants"])


@router.get("", response_model=list[schemas.MerchantOut])
async def list_merchants(
    q: str | None = Query(None, description="Search MID, merchant_name, entity_name"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    query = select(models.Merchant)
    if q:
        like = f"%{q}%"
        query = query.where(
            or_(
                models.Merchant.mid.ilike(like),
                models.Merchant.merchant_name.ilike(like),
                models.Merchant.entity_name.ilike(like),
            )
        )
    query = query.order_by(desc(models.Merchant.first_seen_at)).limit(limit).offset(offset)
    rows = (await db.execute(query)).scalars().all()
    return [merchant_out(m) for m in rows]


@router.get("/new-unlinked", response_model=list[schemas.MerchantOut])
async def list_new_merchants_without_easebuzz_row(
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Merchants from the Gokwik list with no matching Easebuzz row yet.

    Joined on normalized merchant name. Ordered by first_seen_at DESC so the
    newest additions float to the top — the "new arrivals" view.
    """
    eb_names = select(models.EasebuzzOnboarding.name_normalized)
    query = (
        select(models.Merchant)
        .where(
            models.Merchant.name_normalized.is_not(None),
            models.Merchant.name_normalized != "",
            ~models.Merchant.name_normalized.in_(eb_names),
        )
        .order_by(desc(models.Merchant.first_seen_at))
        .limit(limit)
    )
    rows = (await db.execute(query)).scalars().all()
    return [merchant_out(m) for m in rows]


@router.get("/{mid}", response_model=schemas.MerchantOut)
async def get_merchant_by_mid(mid: str, db: AsyncSession = Depends(get_db)):
    row = (
        await db.execute(select(models.Merchant).where(models.Merchant.mid == mid))
    ).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "Merchant not found")
    return merchant_out(row)
