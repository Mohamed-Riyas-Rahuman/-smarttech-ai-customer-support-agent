"""Customer management router."""
from typing import Any
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from app.api.dependencies import get_current_user, get_db, require_agent_or_above
from app.models.customer import Customer
from app.models.device import Device
from app.models.order import Order
from app.models.ticket import SupportTicket
from app.models.user import User
from app.schemas.customer import CustomerCreate, CustomerDetailOut, CustomerOut, CustomerUpdate
from app.services.customer_import import (
    execute_import,
    generate_sample_csv_template,
    parse_file_content,
    validate_and_preview,
)

router = APIRouter(prefix="/api/customers", tags=["Customers"])


@router.get("", response_model=list[CustomerOut])
def list_customers(
    q: str | None = Query(None, description="Search by name, email, or company"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List customers with optional search and pagination."""
    query = select(Customer)
    if q:
        term = f"%{q.lower()}%"
        query = query.where(
            or_(
                func.lower(Customer.full_name).like(term),
                func.lower(Customer.email).like(term),
                func.lower(Customer.company).like(term),
            )
        )
    return db.scalars(query.order_by(Customer.id.asc()).offset(skip).limit(limit)).all()


class ConfirmImportPayload(BaseModel):
    rows: list[dict[str, Any]]


@router.get("/import/template")
def download_import_template(
    current_user: User = Depends(require_agent_or_above),
):
    """Download standard CSV import template."""
    content = generate_sample_csv_template()
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=customer_import_template.csv"},
    )


@router.post("/import/preview")
async def preview_customer_import(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_agent_or_above),
):
    """Upload and preview CSV or Excel customer data before importing."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file uploaded.")
    lower_fn = file.filename.lower()
    if not (lower_fn.endswith(".csv") or lower_fn.endswith(".xlsx") or lower_fn.endswith(".xls")):
        raise HTTPException(status_code=400, detail="Unsupported file format. Please upload a .csv or .xlsx file.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    try:
        raw_rows = parse_file_content(content, file.filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse file: {str(e)}")

    if not raw_rows:
        raise HTTPException(status_code=400, detail="No data rows found in the uploaded file.")

    return validate_and_preview(raw_rows, db)


@router.post("/import/confirm")
async def confirm_customer_import(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_agent_or_above),
):
    """
    Execute transactional customer import.
    Accepts either:
    1. A JSON body conforming to ConfirmImportPayload (e.g. {"rows": [...]})
    2. A multipart/form-data file upload (re-validates and imports valid rows)
    Only records classified as 'valid' are imported; duplicates and invalids are skipped.
    """
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        try:
            body = await request.json()
            payload = ConfirmImportPayload(**body)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {str(e)}")

        valid_rows = payload.rows
        if not valid_rows:
            return {
                "status": "no_valid_records",
                "message": "No valid records to import.",
                "imported_customers": 0,
                "imported_orders": 0,
                "imported_order_items": 0,
                "imported_devices": 0,
                "imported_warranties": 0,
                "skipped_duplicates": 0,
                "invalid_records": 0,
            }

        try:
            result = execute_import(valid_rows, db)
            result.setdefault("skipped_duplicates", 0)
            result.setdefault("invalid_records", 0)
            return result
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Import transaction failed: {str(e)}")

    # Otherwise, handle as multipart form upload
    form = await request.form()
    file = form.get("file")
    if not file or not hasattr(file, "filename") or not file.filename:
        raise HTTPException(status_code=400, detail="No file uploaded.")

    lower_fn = file.filename.lower()
    if not (lower_fn.endswith(".csv") or lower_fn.endswith(".xlsx") or lower_fn.endswith(".xls")):
        raise HTTPException(status_code=400, detail="Unsupported file format. Please upload a .csv or .xlsx file.")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    try:
        raw_rows = parse_file_content(content, file.filename)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse file: {str(e)}")

    if not raw_rows:
        raise HTTPException(status_code=400, detail="No data rows found in the uploaded file.")

    preview = validate_and_preview(raw_rows, db)
    valid_rows = preview.get("valid_rows", [])

    if not valid_rows:
        return {
            "status": "no_valid_records",
            "message": "No valid records to import.",
            "imported_customers": 0,
            "imported_orders": 0,
            "imported_order_items": 0,
            "imported_devices": 0,
            "imported_warranties": 0,
            "skipped_duplicates": preview.get("duplicate_count", 0),
            "invalid_records": preview.get("invalid_count", 0),
        }

    try:
        result = execute_import(valid_rows, db)
        result["skipped_duplicates"] = preview.get("duplicate_count", 0)
        result["invalid_records"] = preview.get("invalid_count", 0)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Import transaction failed: {str(e)}")



@router.get("/{customer_id}", response_model=CustomerDetailOut)
def get_customer(
    customer_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retrieve detailed customer profile including relationship counts."""
    cust = db.scalar(select(Customer).where(Customer.id == customer_id))
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")

    order_count = db.scalar(select(func.count(Order.id)).where(Order.customer_id == customer_id)) or 0
    device_count = db.scalar(select(func.count(Device.id)).where(Device.customer_id == customer_id)) or 0
    ticket_count = db.scalar(select(func.count(SupportTicket.id)).where(SupportTicket.customer_id == customer_id)) or 0

    detail = CustomerDetailOut.model_validate(cust)
    detail.order_count = order_count
    detail.device_count = device_count
    detail.ticket_count = ticket_count
    return detail


@router.post("", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
def create_customer(
    payload: CustomerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_agent_or_above),
):
    """Create a new customer profile."""
    existing = db.scalar(select(Customer).where(Customer.email == payload.email.lower()))
    if existing:
        raise HTTPException(status_code=400, detail="Customer with this email already exists")

    cust = Customer(
        email=payload.email.lower(),
        full_name=payload.full_name,
        phone=payload.phone,
        company=payload.company,
        address=payload.address,
        notes=payload.notes,
    )
    db.add(cust)
    db.commit()
    db.refresh(cust)
    return cust


@router.patch("/{customer_id}", response_model=CustomerOut)
def update_customer(
    customer_id: int,
    payload: CustomerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_agent_or_above),
):
    """Update customer details."""
    cust = db.scalar(select(Customer).where(Customer.id == customer_id))
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")

    for field, val in payload.model_dump(exclude_unset=True).items():
        setattr(cust, field, val)

    db.commit()
    db.refresh(cust)
    return cust
