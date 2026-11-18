"""Product Pydantic schemas."""
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class ProductBase(BaseModel):
    sku: str
    name: str
    category: str
    description: str | None = None
    price: float = 0.0
    warranty_months: int = 12
    support_status: str = "active"
    release_year: int | None = None


class ProductCreate(ProductBase):
    pass


class ProductUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    description: str | None = None
    price: float | None = None
    warranty_months: int | None = None
    support_status: str | None = None
    release_year: int | None = None


class ProductOut(ProductBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
