"""Device Pydantic schemas."""
from datetime import datetime
from pydantic import BaseModel, ConfigDict
from app.schemas.product import ProductOut


class DeviceBase(BaseModel):
    serial_number: str
    customer_id: int
    product_id: int
    order_id: int | None = None
    nickname: str | None = None
    is_registered: bool = True


class DeviceCreate(DeviceBase):
    purchase_date: datetime | None = None


class DeviceOut(DeviceBase):
  model_config = ConfigDict(from_attributes=True)

  id: int
  
  purchase_date: datetime
  created_at: datetime
  product: ProductOut | None = None
  warranty_status: str | None = None
  warranty_end_date: datetime | None = None

class DeviceDetailOut(DeviceOut):
   customer_name: str | None = None