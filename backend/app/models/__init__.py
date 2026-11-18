"""All domain models imported for Alembic and SQLAlchemy metadata."""
from app.db.base import Base
from app.models.user import User
from app.models.customer import Customer
from app.models.product import Product
from app.models.order import Order, OrderItem
from app.models.device import Device
from app.models.warranty import Warranty
from app.models.ticket import SupportTicket, TicketMessage
from app.models.email import EmailMessage
from app.models.draft import Draft
from app.models.knowledge import KnowledgeDocument, KnowledgeChunk
from app.models.agent import AgentExecution, ToolExecution
from app.models.dashboard import DashboardSnapshot
from app.models.audit import AuditLog, SystemSetting

__all__ = [
    "Base",
    "User",
    "Customer",
    "Product",
    "Order",
    "OrderItem",
    "Device",
    "Warranty",
    "SupportTicket",
    "TicketMessage",
    "EmailMessage",
    "Draft",
    "KnowledgeDocument",
    "KnowledgeChunk",
    "AgentExecution",
    "ToolExecution",
    "DashboardSnapshot",
    "AuditLog",
    "SystemSetting",
]
