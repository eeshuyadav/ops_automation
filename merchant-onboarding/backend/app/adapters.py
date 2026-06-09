"""ORM → Pydantic conversions."""
from __future__ import annotations

from app import models, schemas


def merchant_out(m: models.Merchant) -> schemas.MerchantOut:
    return schemas.MerchantOut(
        id=str(m.id),
        mid=m.mid,
        eb_go_live_date=m.eb_go_live_date,
        kyc_spoc=m.kyc_spoc,
        gokwik_kyc_complete_date=m.gokwik_kyc_complete_date,
        merchant_name=m.merchant_name,
        entity_name=m.entity_name,
        email=m.email,
        website=m.website,
        onboarding=m.onboarding,
        entity=m.entity,
        first_seen_at=m.first_seen_at.isoformat(),
        last_synced_at=m.last_synced_at.isoformat(),
    )


def easebuzz_out(e: models.EasebuzzOnboarding) -> schemas.EasebuzzOut:
    return schemas.EasebuzzOut(
        id=str(e.id),
        merchant_id=str(e.merchant_id) if e.merchant_id else None,
        merchant_name=e.merchant_name,
        merchant_size=e.merchant_size,
        onboarding_status=e.onboarding_status,
        kickstart_date=e.kickstart_date,
        kickstart_time=e.kickstart_time,
        docs_received_date=e.docs_received_date,
        docs_received_time=e.docs_received_time,
        days_taken_ks_to_ds=e.days_taken_ks_to_ds,
        time_taken_ks_to_ds=e.time_taken_ks_to_ds,
        kyc_completed_by_ops=e.kyc_completed_by_ops,
        days_taken_kyc=e.days_taken_kyc,
        date_email_sent_to_eb=e.date_email_sent_to_eb,
        salt_key_receipt=e.salt_key_receipt,
        time_taken_by_eb=e.time_taken_by_eb,
        salt_key_from_docs_recd=e.salt_key_from_docs_recd,
        salt_key_from_kickstart=e.salt_key_from_kickstart,
        reasons_for_delay_in_eb=e.reasons_for_delay_in_eb,
        promise=e.promise,
        delivery=e.delivery,
        remarks=e.remarks,
        delay_at_gk=e.delay_at_gk,
        delay_by_merchant=e.delay_by_merchant,
        ops_remarks=e.ops_remarks,
        source=e.source,
        last_edited_in_dashboard_at=(
            e.last_edited_in_dashboard_at.isoformat()
            if e.last_edited_in_dashboard_at else None
        ),
        last_synced_at=e.last_synced_at.isoformat(),
    )


