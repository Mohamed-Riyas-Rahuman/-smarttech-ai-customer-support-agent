"""Customer data import service: CSV and Excel (.xlsx) parsing, validation, duplicate detection, and transactional ingestion."""
import csv
import io
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.models.customer import Customer
from app.models.device import Device
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.warranty import Warranty

# Standard column mapping with case-insensitive and whitespace-tolerant aliases
COLUMN_ALIASES = {
    # Customer
    "email": ["email", "customer_email", "customer email", "e-mail", "user_email"],
    "full_name": ["full_name", "name", "customer_name", "customer name", "client_name"],
    "phone": ["phone", "phone_number", "phone number", "telephone", "mobile"],
    "company": ["company", "company_name", "organization", "business"],
    "address": ["address", "customer_address", "street_address", "billing_address"],
    "notes": ["notes", "customer_notes", "comments"],
    # Product
    "product_sku": ["product_sku", "sku", "item_sku", "product_code"],
    "product_name": ["product_name", "product", "item_name"],
    # Order
    "order_number": ["order_number", "order #", "order_id", "order id", "ordernumber"],
    "order_status": ["order_status", "status", "shipment_status"],
    "total_amount": ["total_amount", "order_total", "total", "amount", "price_total"],
    "carrier": ["carrier", "shipping_carrier", "courier"],
    "tracking_number": ["tracking_number", "tracking #", "tracking", "tracking_code"],
    "order_date": ["order_date", "date_ordered", "purchase_order_date"],
    "shipping_address": ["shipping_address", "delivery_address", "ship_to"],
    # Order Item
    "item_quantity": ["item_quantity", "quantity", "qty", "item_qty"],
    "item_price": ["item_price", "unit_price", "price_each"],
    # Device
    "device_serial": ["device_serial", "serial_number", "serial #", "serial", "device_sn"],
    "device_nickname": ["device_nickname", "nickname", "device_name"],
    "purchase_date": ["purchase_date", "device_purchase_date", "registration_date"],
    # Warranty
    "warranty_policy": ["warranty_policy", "policy_number", "warranty_policy_number", "policy #"],
    "coverage_type": ["coverage_type", "warranty_coverage", "coverage", "warranty_type"],
    "warranty_status": ["warranty_status", "warranty_state"],
    "warranty_start": ["warranty_start", "start_date", "warranty_start_date"],
    "warranty_end": ["warranty_end", "end_date", "warranty_end_date", "expiration_date"],
    "warranty_terms": ["warranty_terms", "terms_summary", "terms"],
}


def _parse_date(val: Any) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val

    s = str(val).strip()
    if not s:
        return None

    # Try common formats
    formats = [
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y",
        "%d/%m/%Y",
        "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(s[:19], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    return None


def _parse_float(val: Any, default: float = 0.0) -> float:
    if val is None or val == "":
        return default
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return default


def _parse_int(val: Any, default: int = 1) -> int:
    if val is None or val == "":
        return default
    if isinstance(val, int):
        return val
    try:
        return int(float(str(val).strip()))
    except ValueError:
        return default


def parse_file_content(file_bytes: bytes, filename: str) -> List[Dict[str, Any]]:
    """Parse raw bytes of a CSV or XLSX file into list of row dictionaries."""
    lower_fn = filename.lower()
    raw_rows: List[Dict[str, Any]] = []

    if lower_fn.endswith(".csv"):
        text_content = file_bytes.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text_content))
        header_row = None
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            if header_row is None:
                header_row = [h.strip() for h in row]
            else:
                row_dict = {}
                for idx, col in enumerate(header_row):
                    val = row[idx].strip() if idx < len(row) else ""
                    row_dict[col] = val
                raw_rows.append(row_dict)

    elif lower_fn.endswith(".xlsx") or lower_fn.endswith(".xls"):
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
        sheet = wb.active
        header_row = None
        for row in sheet.iter_rows(values_only=True):
            if not any(row):
                continue
            if header_row is None:
                header_row = [str(cell).strip() if cell is not None else "" for cell in row]
            else:
                row_dict = {}
                for idx, col in enumerate(header_row):
                    if not col:
                        continue
                    val = row[idx] if idx < len(row) else ""
                    row_dict[col] = val if val is not None else ""
                raw_rows.append(row_dict)
    else:
        raise ValueError("Unsupported file format. Please upload a .csv or .xlsx file.")

    return raw_rows


def normalize_row_keys(raw_row: Dict[str, Any]) -> Dict[str, Any]:
    """Map raw column headers to canonical internal field names."""
    normalized: Dict[str, Any] = {}
    lower_raw = {k.strip().lower(): v for k, v in raw_row.items()}

    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias.lower() in lower_raw:
                val = lower_raw[alias.lower()]
                if val is not None:
                    normalized[canonical] = str(val).strip() if isinstance(val, str) else val
                break
        if canonical not in normalized:
            normalized[canonical] = None

    return normalized


def validate_and_preview(
    raw_rows: List[Dict[str, Any]],
    db: Session,
) -> Dict[str, Any]:
    """Validate all rows and build a comprehensive preview showing valid, duplicate, and invalid records."""
    preview_rows = []
    invalid_rows = []
    duplicate_rows = []
    valid_rows = []

    # Pre-load existing database identifiers for fast duplicate checking
    existing_emails = set(db.scalars(select(func.lower(Customer.email))).all())
    existing_orders = set(db.scalars(select(func.lower(Order.order_number))).all())
    existing_serials = set(db.scalars(select(func.lower(Device.serial_number))).all())
    existing_policies = set(db.scalars(select(func.lower(Warranty.policy_number))).all())

    # Pre-load products
    products = {p.sku.upper(): p for p in db.scalars(select(Product)).all()}

    # Track seen within the file to catch in-file duplicates
    file_emails: set[str] = set()
    file_orders: set[str] = set()
    file_serials: set[str] = set()
    file_policies: set[str] = set()

    email_regex = re.compile(r"^[\w\.\+\-]+@[a-zA-Z0-9\.\-]+\.[a-zA-Z]{2,}$")

    for idx, raw in enumerate(raw_rows, start=1):
        norm = normalize_row_keys(raw)
        errors = []
        warnings = []
        is_dup = False
        dup_reasons = []

        email = str(norm.get("email") or "").strip().lower()
        full_name = str(norm.get("full_name") or "").strip()
        order_num = str(norm.get("order_number") or "").strip().upper()
        serial = str(norm.get("device_serial") or "").strip().upper()
        policy = str(norm.get("warranty_policy") or "").strip().upper()
        sku = str(norm.get("product_sku") or "").strip().upper()

        # 1. Required customer fields
        if not email:
            errors.append("Customer email is required.")
        elif not email_regex.match(email):
            errors.append(f"Invalid email format: '{email}'")

        if not full_name:
            errors.append("Customer full name is required.")

        # 2. Product validation
        if sku and sku not in products:
            errors.append(f"Product SKU '{sku}' not found in catalog (available: {', '.join(products.keys())}).")

        # 3. Duplicate checks
        if email:
            if email in file_emails:
                dup_reasons.append(f"Duplicate customer email in file: {email}")
                is_dup = True
            elif email in existing_emails:
                dup_reasons.append(f"Customer email already exists in database: {email}")
                is_dup = True
            else:
                file_emails.add(email)

        if order_num:
            if order_num in file_orders:
                dup_reasons.append(f"Duplicate order number in file: {order_num}")
                is_dup = True
            elif order_num.lower() in existing_orders:
                dup_reasons.append(f"Order number already exists in database: {order_num}")
                is_dup = True
            else:
                file_orders.add(order_num)

        if serial:
            if serial in file_serials:
                dup_reasons.append(f"Duplicate device serial number in file: {serial}")
                is_dup = True
            elif serial.lower() in existing_serials:
                dup_reasons.append(f"Device serial number already exists in database: {serial}")
                is_dup = True
            else:
                file_serials.add(serial)

        if policy:
            if policy in file_policies:
                dup_reasons.append(f"Duplicate warranty policy in file: {policy}")
                is_dup = True
            elif policy.lower() in existing_policies:
                dup_reasons.append(f"Warranty policy already exists in database: {policy}")
                is_dup = True
            else:
                file_policies.add(policy)

        # 4. Status determination
        if errors:
            status = "invalid"
            invalid_rows.append({"row_number": idx, "email": email, "errors": errors, "data": norm})
        elif is_dup:
            status = "duplicate"
            duplicate_rows.append({"row_number": idx, "email": email, "reasons": dup_reasons, "data": norm})
        else:
            status = "valid"
            valid_rows.append({"row_number": idx, "data": norm})

        preview_rows.append({
            "row_number": idx,
            "status": status,
            "customer_name": full_name,
            "customer_email": email,
            "order_number": order_num or None,
            "product_sku": sku or None,
            "device_serial": serial or None,
            "warranty_policy": policy or None,
            "issues": errors + dup_reasons,
        })

    return {
        "total_rows": len(raw_rows),
        "valid_count": len(valid_rows),
        "duplicate_count": len(duplicate_rows),
        "invalid_count": len(invalid_rows),
        "can_import": len(valid_rows) > 0,
        "preview": preview_rows[:50],  # preview up to first 50
        "duplicate_details": duplicate_rows,
        "invalid_details": invalid_rows,
        "valid_rows": [r["data"] for r in valid_rows],
    }


def execute_import(
    valid_rows: List[Dict[str, Any]],
    db: Session,
) -> Dict[str, Any]:
    """
    Import validated customer records inside a database transaction.
    Maintains clean relationships across Customers, Orders, OrderItems, Devices, and Warranties.
    """
    imported_customers = 0
    imported_orders = 0
    imported_order_items = 0
    imported_devices = 0
    imported_warranties = 0

    products_cache = {p.sku.upper(): p for p in db.scalars(select(Product)).all()}

    try:
        for norm in valid_rows:
            email = str(norm.get("email") or "").strip().lower()
            full_name = str(norm.get("full_name") or "").strip()
            if not email or not full_name:
                continue

            # 1. Customer
            customer = db.scalar(select(Customer).where(func.lower(Customer.email) == email))
            if not customer:
                customer = Customer(
                    email=email,
                    full_name=full_name,
                    phone=norm.get("phone"),
                    company=norm.get("company"),
                    address=norm.get("address"),
                    notes=norm.get("notes"),
                )
                db.add(customer)
                db.flush()
                imported_customers += 1

            # 2. Product reference
            sku = str(norm.get("product_sku") or "").strip().upper()
            product = products_cache.get(sku)

            # 3. Order
            order_num = str(norm.get("order_number") or "").strip().upper()
            order = None
            if order_num:
                order = db.scalar(select(Order).where(func.lower(Order.order_number) == order_num.lower()))
                if not order:
                    order_date = _parse_date(norm.get("order_date")) or datetime.now(timezone.utc)
                    order = Order(
                        order_number=order_num,
                        customer_id=customer.id,
                        status=str(norm.get("order_status") or "delivered").lower(),
                        total_amount=_parse_float(norm.get("total_amount")),
                        shipping_address=norm.get("shipping_address") or norm.get("address"),
                        carrier=norm.get("carrier") or "FedEx",
                        tracking_number=norm.get("tracking_number"),
                        order_date=order_date,
                    )
                    db.add(order)
                    db.flush()
                    imported_orders += 1

                    # Add OrderItem if product is known
                    if product:
                        qty = _parse_int(norm.get("item_quantity"), default=1)
                        unit_p = _parse_float(norm.get("item_price"), default=float(product.price or 0.0))
                        item = OrderItem(
                            order_id=order.id,
                            product_id=product.id,
                            quantity=qty,
                            unit_price=unit_p,
                            serial_number=str(norm.get("device_serial") or "").strip().upper() or None,
                        )
                        db.add(item)
                        db.flush()
                        imported_order_items += 1

            # 4. Device
            serial = str(norm.get("device_serial") or "").strip().upper()
            device = None
            if serial and product:
                device = db.scalar(select(Device).where(func.lower(Device.serial_number) == serial.lower()))
                if not device:
                    purchase_date = _parse_date(norm.get("purchase_date")) or datetime.now(timezone.utc)
                    device = Device(
                        serial_number=serial,
                        customer_id=customer.id,
                        product_id=product.id,
                        order_id=order.id if order else None,
                        nickname=norm.get("device_nickname"),
                        purchase_date=purchase_date,
                        is_registered=True,
                    )
                    db.add(device)
                    db.flush()
                    imported_devices += 1

            # 5. Warranty
            policy = str(norm.get("warranty_policy") or "").strip().upper()
            if policy and device:
                warranty = db.scalar(select(Warranty).where(func.lower(Warranty.policy_number) == policy.lower()))
                if not warranty:
                    start_date = _parse_date(norm.get("warranty_start")) or datetime.now(timezone.utc)
                    end_date = _parse_date(norm.get("warranty_end"))
                    if not end_date:
                        # Default to 24 months from start
                        end_date = datetime(start_date.year + 2, start_date.month, start_date.day, tzinfo=timezone.utc)

                    now = datetime.now(timezone.utc)
                    w_status = str(norm.get("warranty_status") or ("active" if end_date > now else "expired")).lower()

                    warranty = Warranty(
                        policy_number=policy,
                        device_id=device.id,
                        customer_id=customer.id,
                        coverage_type=str(norm.get("coverage_type") or "standard").lower(),
                        status=w_status,
                        start_date=start_date,
                        end_date=end_date,
                        terms_summary=norm.get("warranty_terms") or "Standard manufacturer limited hardware warranty.",
                    )
                    db.add(warranty)
                    db.flush()
                    imported_warranties += 1

        db.commit()
        return {
            "status": "success",
            "imported_customers": imported_customers,
            "imported_orders": imported_orders,
            "imported_order_items": imported_order_items,
            "imported_devices": imported_devices,
            "imported_warranties": imported_warranties,
            "message": f"Successfully imported {imported_customers} customer(s), {imported_orders} order(s), {imported_devices} device(s), and {imported_warranties} warranty policy(ies).",
        }
    except Exception as e:
        db.rollback()
        logger.error("Customer import transaction failed (%s).", type(e).__name__)
        raise


def generate_sample_csv_template() -> str:
    """Generate a clean CSV template string with headers and realistic example rows."""
    headers = [
        "customer_name",
        "customer_email",
        "phone",
        "company",
        "address",
        "product_sku",
        "order_number",
        "order_status",
        "total_amount",
        "carrier",
        "tracking_number",
        "order_date",
        "item_quantity",
        "item_price",
        "device_serial",
        "device_nickname",
        "purchase_date",
        "warranty_policy",
        "coverage_type",
        "warranty_status",
        "warranty_start",
        "warranty_end",
        "warranty_terms",
    ]
    sample_rows = [
        [
            "Sarah Jenkins",
            "sarah.jenkins@example.com",
            "+1-555-0144",
            "Apex Innovations",
            "100 Innovation Way, Austin, TX 78701",
            "ST-AUD-001",
            "ORD-2026-9011",
            "delivered",
            "249.99",
            "FedEx",
            "TRK-FDX-901122",
            "2026-03-10",
            "1",
            "249.99",
            "ST-SN-HP-9011",
            "Sarah's SoundPulse",
            "2026-03-10",
            "WAR-AUD-9011",
            "standard",
            "active",
            "2026-03-10",
            "2028-03-10",
            "Standard 24-month manufacturer audio warranty.",
        ],
        [
            "Michael Chang",
            "michael.chang@example.com",
            "+1-555-0188",
            "Horizon Media",
            "450 Sunset Blvd, Los Angeles, CA 90028",
            "ST-VIS-002",
            "ORD-2026-9012",
            "delivered",
            "599.99",
            "UPS",
            "TRK-UPS-901233",
            "2024-02-15",
            "1",
            "599.99",
            "ST-SN-VIS-9012",
            "Conference Room Projector",
            "2024-02-15",
            "WAR-VIS-9012",
            "standard",
            "expired",
            "2024-02-15",
            "2025-02-15",
            "Standard 12-month manufacturer display warranty.",
        ],
        [
            "Elena Rostova",
            "elena.rostova@example.com",
            "+1-555-0199",
            "Quantum Tech",
            "742 Evergreen Terrace, Springfield, OR 97477",
            "ST-CMP-003",
            "ORD-2026-9013",
            "shipped",
            "1199.00",
            "USPS",
            "TRK-USPS-901344",
            "2026-04-01",
            "1",
            "1199.00",
            "ST-SN-LAP-9013",
            "Work Laptop",
            "2026-04-01",
            "WAR-CMP-9013",
            "extended",
            "active",
            "2026-04-01",
            "2029-04-01",
            "Extended 36-month premium computing warranty.",
        ],
    ]

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers)
    for r in sample_rows:
        writer.writerow(r)
    return out.getvalue()
