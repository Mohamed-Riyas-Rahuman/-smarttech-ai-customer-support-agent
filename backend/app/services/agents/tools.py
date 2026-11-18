"""Tool implementations for AI Support Agent: Customer, Order, Device, Product, Warranty, Knowledge, and Email operations."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.logging import logger
from app.models.customer import Customer
from app.models.device import Device
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.ticket import SupportTicket
from app.models.warranty import Warranty
from app.services.rag.retrieval import identify_product_id, search_knowledge_base


def tool_get_customer_details(
    db: Session,
    customer_id: int | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    """Look up customer record, contact info, and registration history."""
    if customer_id is None and not email:
        return {
            "found": False,
            "error": "customer_id or email must be provided",
        }

    query = select(Customer)

    if customer_id is not None:
        query = query.where(Customer.id == customer_id)
    elif email:
        query = query.where(
            func.lower(Customer.email) == email.strip().lower()
        )

    cust = db.scalar(query)

    if not cust:
        return {
            "found": False,
            "message": "Customer not found in database.",
        }

    return {
        "found": True,
        "id": cust.id,
        "name": cust.full_name,
        "email": cust.email,
        "phone": cust.phone,
        "company": cust.company,
        "address": cust.address,
    }


def tool_get_customer_orders(
    db: Session,
    customer_id: int,
) -> list[dict[str, Any]]:
    """Retrieve orders belonging only to the specified customer."""
    if customer_id is None:
        return []

    orders = db.scalars(
        select(Order)
        .options(
            selectinload(Order.items).selectinload(OrderItem.product)
        )
        .where(Order.customer_id == customer_id)
        .order_by(Order.order_date.desc())
    ).all()

    results = []

    for order in orders:
        items = [
            {
                "product_name": (
                    item.product.name if item.product else "Unknown Product"
                ),
                "sku": item.product.sku if item.product else None,
                "qty": item.quantity,
                "serial_number": item.serial_number,
            }
            for item in order.items
            if item
        ]

        results.append(
            {
                "order_number": order.order_number,
                "status": order.status,
                "order_date": (
                    order.order_date.strftime("%Y-%m-%d")
                    if order.order_date
                    else None
                ),
                "carrier": order.carrier,
                "tracking_number": order.tracking_number,
                "items": items,
            }
        )

    return results


def tool_get_registered_devices(
    db: Session,
    customer_id: int,
) -> list[dict[str, Any]]:
    """Retrieve devices registered to the specified customer."""
    if customer_id is None:
        return []

    devices = db.scalars(
        select(Device)
        .options(
            selectinload(Device.product),
            selectinload(Device.warranty),
        )
        .where(Device.customer_id == customer_id)
    ).all()

    results = []

    for device in devices:
        warranty_status = (
            device.warranty.status
            if device.warranty
            else "no_warranty"
        )

        warranty_end = (
            device.warranty.end_date.strftime("%Y-%m-%d")
            if device.warranty and device.warranty.end_date
            else None
        )

        results.append(
            {
                "serial_number": device.serial_number,
                "device_id": device.id,
                "product_id": device.product_id,
                "product_name": (
                    device.product.name if device.product else "Unknown"
                ),
                "sku": device.product.sku if device.product else None,
                "nickname": device.nickname,
                "purchase_date": (
                    device.purchase_date.strftime("%Y-%m-%d")
                    if device.purchase_date
                    else None
                ),
                "warranty_status": warranty_status,
                "warranty_end_date": warranty_end,
            }
        )

    return results


def tool_get_device_by_serial(
    db: Session,
    serial_number: str,
    customer_id: int,
) -> dict[str, Any]:
    """
    Look up a device only when its serial number belongs to the customer.

    Customer ownership is enforced inside the database query.
    """
    if (
        not serial_number
        or not serial_number.strip()
        or customer_id is None
    ):
        return {
            "found": False,
            "message": "Device not found for this customer.",
        }

    cleaned_serial = serial_number.strip()

    device = db.scalar(
        select(Device)
        .options(
            selectinload(Device.product),
            selectinload(Device.warranty),
        )
        .where(
            func.lower(Device.serial_number) == cleaned_serial.lower(),
            Device.customer_id == customer_id,
        )
    )

    if not device:
        return {
            "found": False,
            "message": "Device not found for this customer.",
        }

    warranty_status = (
        device.warranty.status
        if device.warranty
        else "no_warranty"
    )

    warranty_end = (
        device.warranty.end_date.strftime("%Y-%m-%d")
        if device.warranty and device.warranty.end_date
        else None
    )

    return {
        "found": True,
        "device_id": device.id,
        "serial_number": device.serial_number,
        "product_id": device.product_id,
        "product_name": (
            device.product.name if device.product else "Unknown"
        ),
        "sku": device.product.sku if device.product else None,
        "customer_id": device.customer_id,
        "warranty_status": warranty_status,
        "warranty_end_date": warranty_end,
        "purchase_date": (
            device.purchase_date.strftime("%Y-%m-%d")
            if device.purchase_date
            else None
        ),
    }


def tool_get_product_specifications(
    db: Session,
    sku: str | None = None,
    product_name: str | None = None,
    product_id: int | None = None,
) -> dict[str, Any]:
    """Retrieve public product specifications from the product catalog."""
    if not sku and not product_name and product_id is None:
        return {
            "found": False,
            "error": "sku, product_name, or product_id must be provided",
        }

    query = select(Product)

    if product_id is not None:
        query = query.where(Product.id == product_id)
    elif sku:
        query = query.where(
            func.lower(Product.sku) == sku.strip().lower()
        )
    elif product_name:
        term = f"%{product_name.strip().lower()}%"
        query = query.where(
            func.lower(Product.name).like(term)
        )

    product = db.scalar(query)

    if not product:
        return {
            "found": False,
            "message": "Product specifications not found in catalog.",
        }

    return {
        "found": True,
        "id": product.id,
        "sku": product.sku,
        "name": product.name,
        "category": product.category,
        "description": product.description,
        "price": (
            float(product.price)
            if product.price is not None
            else 0.0
        ),
        "warranty_months": product.warranty_months,
        "support_status": product.support_status,
        "release_year": product.release_year,
    }


def tool_get_warranty_details(
    db: Session,
    serial_number: str,
    customer_id: int | None = None,
) -> dict[str, Any]:
    """
    Retrieve warranty details only for the specified customer's device.

    Both serial number and customer ID must match if customer_id is provided.
    """
    if not serial_number or not serial_number.strip():
        return {
            "has_warranty": False,
            "error": "serial_number must be provided",
            "message": "No warranty found.",
        }

    cleaned_serial = serial_number.strip()
    query = (
        select(Device)
        .options(selectinload(Device.warranty))
        .where(func.lower(Device.serial_number) == cleaned_serial.lower())
    )
    if customer_id is not None:
        query = query.where(Device.customer_id == customer_id)

    device = db.scalar(query)

    if not device or not device.warranty:
        return {
            "has_warranty": False,
            "error": "No warranty found for this customer.",
            "message": "No warranty found for this customer.",
        }

    warranty = device.warranty
    now = datetime.now(timezone.utc)
    end_date = warranty.end_date

    # Normalize naive database timestamps to UTC before comparison.
    if end_date is not None and end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)

    is_active = (
        warranty.status == "active"
        and end_date is not None
        and end_date > now
    )

    return {
        "has_warranty": True,
        "is_active": is_active,
        "status": warranty.status,
        "policy_number": warranty.policy_number,
        "start_date": (
            warranty.start_date.strftime("%Y-%m-%d")
            if warranty.start_date
            else None
        ),
        "end_date": (
            warranty.end_date.strftime("%Y-%m-%d")
            if warranty.end_date
            else None
        ),
        "coverage_type": warranty.coverage_type,
        "terms_summary": warranty.terms_summary,
    }


def tool_search_knowledge_base(
    db: Session,
    query: str,
    product_id: int | None = None,
    min_similarity: float = 0.40,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """Retrieve relevant evidence from approved support documentation."""
    if not query or not query.strip():
        return []

    if product_id is None:
        product_id = identify_product_id(query, db)

    results = search_knowledge_base(
        query=query,
        db=db,
        product_id=product_id,
        top_k=top_k,
        min_similarity=min_similarity,
    )

    return [
        {
            "chunk_id": result.chunk_id,
            "document_id": result.document_id,
            "citation": result.citation,
            "document": result.document_title,
            "filename": result.filename,
            "similarity": result.similarity,
            "retrieval_method": result.retrieval_method,
            "content": result.content,
        }
        for result in results
    ]

