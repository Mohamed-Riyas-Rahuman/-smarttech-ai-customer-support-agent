"""Warranty Pydantic schemas."""
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class WarrantyBase(BaseModel):
    policy_number: str
    device_id: int
    customer_id: int
    coverage_type: str = "standard"
    status: str = "active"
    start_date: datetime
    end_date: datetime
    terms_summary: str | None = None


class WarrantyCreate(WarrantyBase):
    pass


class WarrantyOut(WarrantyBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    is_valid_currently: bool = True


class WarrantyCheckResponse(BaseModel):
    has_coverage: bool
    status: str
    end_date: datetime | None = None
    policy_number: str | None = None
    terms_summary: str | None = None
    message: str
