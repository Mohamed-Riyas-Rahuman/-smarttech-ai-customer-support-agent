"""Email Pydantic schemas."""
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class EmailOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider_message_id: str
    thread_id: str
    ticket_id: int | None = None
    sender: str
    recipient: str
    subject: str
    body_text: str
    body_html: str | None = None
    is_processed: bool
    received_at: datetime


class EmailSyncResponse(BaseModel):
    status: str
    fetched_count: int
    new_emails_count: int
    created_tickets_count: int
    message: str


class EmailProcessRequest(BaseModel):
    email_id: int
