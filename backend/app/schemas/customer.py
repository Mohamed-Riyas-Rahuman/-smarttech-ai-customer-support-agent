"""Customer Pydantic schemas."""
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class CustomerBase(BaseModel):
    email: str
    full_name: str
    phone: str | None = None
    company: str | None = None
    address: str | None = None
    notes: str | None = None


class CustomerCreate(CustomerBase):
    pass


class CustomerUpdate(BaseModel):
    full_name: str | None = None
    phone: str | None = None
    company: str | None = None
    address: str | None = None
    notes: str | None = None


class CustomerOut(CustomerBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


class CustomerDetailOut(CustomerOut):
    order_count: int = 0
    device_count: int = 0
    ticket_count: int = 0
