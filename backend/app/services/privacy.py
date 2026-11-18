"""Database-backed ownership checks for customer-private product records."""

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.device import Device
from app.models.order import Order
from app.models.ticket import SupportTicket


@dataclass(frozen=True)
class OwnershipMatch:
    record_type: str


_PRIVATE_RECORD_INTENT = re.compile(
    r"\b(?:status|update|progress|repair|service request|registered|serial|"
    r"order|details|ownership|who owns|belongs to|warranty for|tracking)\b",
    re.IGNORECASE,
)


def _contains_exact(text: str, value: str | None) -> bool:
    value = (value or "").strip()
    if len(value) < 3:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(value) + r"(?![a-z0-9])", text, re.I) is not None


def find_cross_customer_record_request(db: Session, *, customer_id: int | None, subject: str, body: str) -> OwnershipMatch | None:
    """Find an explicitly referenced record owned by another verified customer.

    Product model names alone are intentionally not ownership indicators. A
    match requires a private-record intent plus a database relationship tying
    the named identifier/customer to an order, device, or ticket.
    """
    if customer_id is None:
        return None
    text = f"{subject or ''}\n{body or ''}"
    if not _PRIVATE_RECORD_INTENT.search(text):
        return None

    other_customers = db.scalars(select(Customer).where(Customer.id != customer_id)).all()
    mentioned_customer_ids = {
        customer.id for customer in other_customers
        if _contains_exact(text, customer.full_name) or _contains_exact(text, customer.email)
    }
    for other_id in mentioned_customer_ids:
        has_owned_record = (
            db.scalar(select(Device.id).where(Device.customer_id == other_id).limit(1))
            or db.scalar(select(Order.id).where(Order.customer_id == other_id).limit(1))
            or db.scalar(select(SupportTicket.id).where(SupportTicket.customer_id == other_id).limit(1))
        )
        if has_owned_record:
            return OwnershipMatch("customer-linked product or service record")

    other_devices = db.scalars(select(Device).where(Device.customer_id != customer_id)).all()
    for device in other_devices:
        if _contains_exact(text, device.serial_number) or _contains_exact(text, device.nickname):
            return OwnershipMatch("registered device")

    other_orders = db.scalars(select(Order).where(Order.customer_id != customer_id)).all()
    for order in other_orders:
        if _contains_exact(text, order.order_number):
            return OwnershipMatch("order")

    other_tickets = db.scalars(select(SupportTicket).where(
        SupportTicket.customer_id.is_not(None), SupportTicket.customer_id != customer_id
    )).all()
    for ticket in other_tickets:
        if _contains_exact(text, ticket.ticket_number):
            return OwnershipMatch("service ticket")

    return None
