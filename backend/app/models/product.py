"""Product model."""
from datetime import datetime, timezone
from sqlalchemy import DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db.base import Base


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(100), index=True, nullable=False)  # Audio, Display, Computing, SmartHome
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[float] = mapped_column(Numeric(10, 2), default=0.0)
    warranty_months: Mapped[int] = mapped_column(Integer, default=12)
    support_status: Mapped[str] = mapped_column(String(50), default="active")  # active, deprecated, end_of_life
    release_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    order_items = relationship("OrderItem", back_populates="product")
    devices = relationship("Device", back_populates="product")
    knowledge_documents = relationship("KnowledgeDocument", back_populates="product")
