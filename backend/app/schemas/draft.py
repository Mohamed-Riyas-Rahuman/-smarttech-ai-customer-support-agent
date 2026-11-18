"""Email Draft Pydantic schemas."""
from datetime import datetime
import re
from pydantic import BaseModel, ConfigDict, Field, field_validator


class DraftCreate(BaseModel):
    ticket_id: int | None = None
    email_id: int | None = None
    recipient: str
    subject: str
    body: str = Field(..., min_length=1)
    citations: str | None = None
    confidence_score: float | None = None

    @field_validator("body")
    @classmethod
    def validate_body(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Email body must contain customer-facing text.")
        content = value.strip()
        if re.match(r'(?s)^\s*\{\s*"subject"\s*:', content) or re.search(r"(?im)^\s*Sources\s*:\s*$", content):
            raise ValueError("A JSON wrapper or internal source metadata cannot be saved as an email body.")
        if re.search(r"(?i)\b(?:api[_ -]?error|traceback|authentication failed|rate limit exceeded|error\s*[:=])", content):
            raise ValueError("Provider error text cannot be saved as a customer response.")
        if re.match(r"(?im)^\s*(?:source:|citation:|\[evidence\s+\d+\]|relevance:\s*\d+%?)", content):
            raise ValueError("A customer response cannot contain citations without a message body.")
        return content


class DraftUpdate(BaseModel):
    body: str = Field(..., min_length=1)
    subject: str | None = None
    recipient: str | None = None

    @field_validator("body")
    @classmethod
    def validate_body(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Email body must contain customer-facing text.")
        content = value.strip()
        if re.match(r'(?s)^\s*\{\s*"subject"\s*:', content) or re.search(r"(?im)^\s*Sources\s*:\s*$", content):
            raise ValueError("A JSON wrapper or internal source metadata cannot be saved as an email body.")
        if re.search(r"(?i)\b(?:api[_ -]?error|traceback|authentication failed|rate limit exceeded|error\s*[:=])", content):
            raise ValueError("Provider error text cannot be saved as a customer response.")
        if re.match(r"(?im)^\s*(?:source:|citation:|\[evidence\s+\d+\]|relevance:\s*\d+%?)", content):
            raise ValueError("A customer response cannot contain citations without a message body.")
        return content


class DraftApproveRequest(BaseModel):
    pass


class DraftRejectRequest(BaseModel):
    reason: str = Field(..., min_length=3, description="Mandatory explanation for draft rejection")


class DraftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticket_id: int | None = None
    email_id: int | None = None
    recipient: str
    subject: str
    body: str
    status: str
    approved_by: int | None = None
    approved_body_hash: str | None = None
    rejected_by: int | None = None
    rejection_reason: str | None = None
    send_attempts: int
    sent_at: datetime | None = None
    provider_message_id: str | None = None
    citations: str | None = None
    created_at: datetime
    updated_at: datetime


class DraftSendResponse(BaseModel):
    status: str
    draft_id: int
    sent_at: datetime
    mode: str  # live_gmail or simulated
    message: str
