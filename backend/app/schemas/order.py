"""Order and OrderItem Pydantic schemas."""
from datetime import datetime
from pydantic import BaseModel, ConfigDict
from app.schemas.product import ProductOut


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    quantity: int
    unit_price: float
    serial_number: str | None = None
    product: ProductOut | None = None


class OrderBase(BaseModel):
    order_number: str
    customer_id: int
    status: str
    total_amount: float
    shipping_address: str | None = None
    carrier: str | None = None
    tracking_number: str | None = None


class OrderCreate(OrderBase):
    pass


class OrderOut(OrderBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_date: datetime
    delivered_at: datetime | None = None
    created_at: datetime


class OrderDetailOut(OrderOut):
    items: list[OrderItemOut] = []
    customer_name: str | None = None
    customer_email: str | None = None
