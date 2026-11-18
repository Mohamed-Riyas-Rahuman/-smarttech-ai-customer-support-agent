"""SupportTicket and TicketMessage Pydantic schemas."""
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.schemas.customer import CustomerOut


class TicketMessageBase(BaseModel):
    message_body: str
    is_internal_note: bool = False


class TicketMessageCreate(TicketMessageBase):
    pass


class TicketMessageOut(TicketMessageBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_id: int
    sender_type: str
    sender_email: str | None = None
    sender_name: str | None = None
    created_at: datetime


class TicketBase(BaseModel):
    subject: str
    customer_id: int | None = None
    order_id: int | None = None
    device_id: int | None = None
    priority: str = "medium"
    category: str = "general"


class TicketCreate(BaseModel):
    subject: str
    customer_id: int
    order_id: int | None = None
    device_id: int | None = None
    priority: str = "medium"
    category: str = "general"
    initial_message: str | None = None


class TicketUpdate(BaseModel):
    subject: str | None = None
    status: str | None = None
    priority: str | None = None
    category: str | None = None
    assigned_to_user_id: int | None = None
    escalation_status: str | None = None


class TicketOut(TicketBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_number: str
    status: str
    sentiment: str
    escalation_status: str
    assigned_to_user_id: int | None = None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    customer: CustomerOut | None = None
    customer_name: str | None = None
    customer_email: str | None = None

    @model_validator(mode="after")
    def set_customer_info(self) -> "TicketOut":
        if self.customer:
            if not self.customer_name:
                self.customer_name = self.customer.full_name
            if not self.customer_email:
                self.customer_email = self.customer.email
        return self


class TicketDetailOut(TicketOut):
    messages: list[TicketMessageOut] = []
    has_pending_draft: bool = False
