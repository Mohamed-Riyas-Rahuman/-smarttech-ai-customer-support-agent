"""Order management router."""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload
from app.api.dependencies import get_current_user, get_db
from app.models.customer import Customer
from app.models.order import Order, OrderItem
from app.models.user import User
from app.schemas.order import OrderCreate, OrderDetailOut, OrderItemOut, OrderOut

router = APIRouter(prefix="/api/orders", tags=["Orders"])


@router.get("", response_model=list[OrderOut])
def list_orders(
    customer_id: int | None = Query(None, description="Filter by customer ID"),
    status_filter: str | None = Query(None, alias="status", description="Filter by order status"),
    q: str | None = Query(None, description="Search by order number or tracking number"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List customer orders with optional filters."""
    query = select(Order)
    if customer_id:
        query = query.where(Order.customer_id == customer_id)
    if status_filter:
        query = query.where(Order.status == status_filter)
    if q:
        term = f"%{q.lower()}%"
        query = query.where(
            or_(
                func.lower(Order.order_number).like(term),
                func.lower(Order.tracking_number).like(term),
            )
        )
    return db.scalars(query.order_by(Order.order_date.desc()).offset(skip).limit(limit)).all()


@router.get("/{order_id}", response_model=OrderDetailOut)
def get_order(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retrieve detailed order with itemized products and customer details."""
    order = db.scalar(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.product), selectinload(Order.customer))
        .where(Order.id == order_id)
    )
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    detail = OrderDetailOut.model_validate(order)
    if order.customer:
        detail.customer_name = order.customer.full_name
        detail.customer_email = order.customer.email
    return detail


@router.post("", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def create_order(
    payload: OrderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new customer order."""
    existing = db.scalar(select(Order).where(Order.order_number == payload.order_number))
    if existing:
        raise HTTPException(status_code=400, detail="Order with this number already exists")

    order = Order(**payload.model_dump())
    db.add(order)
    db.commit()
    db.refresh(order)
    return order
