"""Customer Registered Devices router with serial number masking."""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload
from app.api.dependencies import get_current_user, get_db
from app.models.device import Device
from app.models.product import Product
from app.models.user import User
from app.models.warranty import Warranty
from app.schemas.device import DeviceCreate, DeviceDetailOut, DeviceOut

router = APIRouter(prefix="/api/devices", tags=["Devices"])


def mask_serial_number(serial: str, role: str) -> str:
    """Mask serial numbers for read-only analysts while revealing to agents, managers, and admins."""
    if role in ("admin", "manager", "agent"):
        return serial
    if len(serial) > 8:
        return f"{serial[:5]}***{serial[-5:]}"
    return "****"


@router.get("", response_model=list[DeviceOut])
def list_devices(
    customer_id: int | None = Query(None, description="Filter by customer ID"),
    q: str | None = Query(None, description="Search by serial number or nickname"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
  ):
    """List registered customer devices with role-based serial number masking."""
    query = select(Device).options(
    selectinload(Device.product),
    selectinload(Device.warranty),
 )
    if customer_id:
        query = query.where(Device.customer_id == customer_id)
    if q:
        term = f"%{q.lower()}%"
        query = query.where(
            or_(
                func.lower(Device.serial_number).like(term),
                func.lower(Device.nickname).like(term),
            )
        )
    devices = db.scalars(query.order_by(Device.id.desc()).offset(skip).limit(limit)).all()

    # Mask serial numbers if user is analyst
    results = []
    for d in devices:
     item = DeviceOut.model_validate(d)
     item.serial_number = mask_serial_number(d.serial_number, current_user.role)

     if d.warranty:
        item.warranty_status = d.warranty.status
        item.warranty_end_date = d.warranty.end_date

     results.append(item)

    return results


@router.get("/{device_id}", response_model=DeviceDetailOut)
def get_device(
    device_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retrieve detailed device info including warranty and product associations."""
    device = db.scalar(
        select(Device)
        .options(selectinload(Device.product), selectinload(Device.customer), selectinload(Device.warranty))
        .where(Device.id == device_id)
    )
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    detail = DeviceDetailOut.model_validate(device)
    detail.serial_number = mask_serial_number(device.serial_number, current_user.role)
    if device.customer:
        detail.customer_name = device.customer.full_name
    if device.warranty:
        detail.warranty_status = device.warranty.status
        detail.warranty_end_date = device.warranty.end_date
    return detail


@router.post("", response_model=DeviceOut, status_code=status.HTTP_201_CREATED)
def register_device(
    payload: DeviceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Register a new device to a customer."""
    existing = db.scalar(select(Device).where(Device.serial_number == payload.serial_number))
    if existing:
        raise HTTPException(status_code=400, detail="Device with this serial number already registered")

    device = Device(**payload.model_dump())
    db.add(device)
    db.commit()
    db.refresh(device)
    return device
