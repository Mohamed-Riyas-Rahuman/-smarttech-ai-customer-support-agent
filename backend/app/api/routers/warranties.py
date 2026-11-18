"""Warranty Management and Coverage Verification router."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload
from app.api.dependencies import get_current_user, get_db
from app.models.device import Device
from app.models.product import Product
from app.models.user import User
from app.models.warranty import Warranty
from app.schemas.warranty import WarrantyCheckResponse, WarrantyOut

router = APIRouter(prefix="/api/warranties", tags=["Warranties"])


@router.get("", response_model=list[WarrantyOut])
def list_warranties(
    status_filter: str | None = Query(None, alias="status", description="Filter by status (active, expired, voided)"),
    customer_id: int | None = Query(None, description="Filter by customer ID"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List warranties with status and customer filters."""
    query = select(Warranty)
    if status_filter:
        query = query.where(Warranty.status == status_filter)
    if customer_id:
        query = query.where(Warranty.customer_id == customer_id)

    warranties = db.scalars(query.order_by(Warranty.end_date.desc()).offset(skip).limit(limit)).all()
    now = datetime.now(timezone.utc)
    results = []
    for w in warranties:
        item = WarrantyOut.model_validate(w)
        item.is_valid_currently = (w.status == "active" and w.end_date > now)
        results.append(item)
    return results


@router.get("/{warranty_id}", response_model=WarrantyOut)
def get_warranty(
    warranty_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retrieve warranty details by ID."""
    w = db.scalar(select(Warranty).where(Warranty.id == warranty_id))
    if not w:
        raise HTTPException(status_code=404, detail="Warranty not found")

    item = WarrantyOut.model_validate(w)
    now = datetime.now(timezone.utc)
    item.is_valid_currently = (w.status == "active" and w.end_date > now)
    return item


@router.get("/check/{identifier}", response_model=WarrantyCheckResponse)
def check_warranty_coverage(
    identifier: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Evidence-based warranty check by Serial Number, Policy Number, or Device ID."""
    now = datetime.now(timezone.utc)
    # Search by serial number, policy number, or device ID
    device = db.scalar(
        select(Device)
        .options(selectinload(Device.warranty), selectinload(Device.product))
        .where(
            or_(
                Device.serial_number == identifier,
                Device.id == int(identifier) if identifier.isdigit() else False,
            )
        )
    )

    if device and device.warranty:
        w = device.warranty
        is_active = (w.status == "active" and w.end_date > now)
        prod_name = device.product.name if device.product else "device"
        if is_active:
            msg = (
                f"Active warranty verified for {prod_name} (SN: {device.serial_number}). "
                f"Policy #{w.policy_number} is valid until {w.end_date.strftime('%Y-%m-%d')}. "
                f"Coverage type: {w.coverage_type}. Hardware repairs/replacements authorized per standard policy."
            )
        else:
            msg = (
                f"Warranty expired on {w.end_date.strftime('%Y-%m-%d')} for {prod_name} "
                f"(Policy #{w.policy_number}). Free warranty service cannot be promised without manager escalation."
            )
        return WarrantyCheckResponse(
            has_coverage=is_active,
            status=w.status if not is_active or w.status != "active" else "active",
            end_date=w.end_date,
            policy_number=w.policy_number,
            terms_summary=w.terms_summary,
            message=msg,
        )

    # Search directly by policy number
    w = db.scalar(select(Warranty).where(Warranty.policy_number == identifier))
    if w:
        is_active = (w.status == "active" and w.end_date > now)
        return WarrantyCheckResponse(
            has_coverage=is_active,
            status=w.status,
            end_date=w.end_date,
            policy_number=w.policy_number,
            terms_summary=w.terms_summary,
            message=f"Policy #{w.policy_number} is {'active' if is_active else 'expired or void'}.",
        )

    return WarrantyCheckResponse(
        has_coverage=False,
        status="not_found",
        message=f"No warranty record found for identifier '{identifier}'. Evidence insufficient to promise warranty coverage.",
    )
