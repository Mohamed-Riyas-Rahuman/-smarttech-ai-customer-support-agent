"""Product catalog router."""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from app.api.dependencies import get_current_user, get_db, require_manager_or_admin
from app.models.product import Product
from app.models.user import User
from app.schemas.product import ProductCreate, ProductOut, ProductUpdate

router = APIRouter(prefix="/api/products", tags=["Products"])


@router.get("", response_model=list[ProductOut])
def list_products(
    q: str | None = Query(None, description="Search by name, SKU, or category"),
    category: str | None = Query(None, description="Filter by category"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List products with optional search and category filtering."""
    query = select(Product)
    if category:
        query = query.where(Product.category == category)
    if q:
        term = f"%{q.lower()}%"
        query = query.where(
            or_(
                func.lower(Product.name).like(term),
                func.lower(Product.sku).like(term),
                func.lower(Product.category).like(term),
            )
        )
    return db.scalars(query.order_by(Product.id.asc()).offset(skip).limit(limit)).all()


@router.get("/{product_id}", response_model=ProductOut)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retrieve product by ID."""
    prod = db.scalar(select(Product).where(Product.id == product_id))
    if not prod:
        raise HTTPException(status_code=404, detail="Product not found")
    return prod


@router.post("", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
def create_product(
    payload: ProductCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_manager_or_admin),
):
    """Create a new product (Manager or Admin only)."""
    existing = db.scalar(select(Product).where(Product.sku == payload.sku))
    if existing:
        raise HTTPException(status_code=400, detail="Product with this SKU already exists")

    prod = Product(**payload.model_dump())
    db.add(prod)
    db.commit()
    db.refresh(prod)
    return prod


@router.patch("/{product_id}", response_model=ProductOut)
def update_product(
    product_id: int,
    payload: ProductUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_manager_or_admin),
):
    """Update product properties (Manager or Admin only)."""
    prod = db.scalar(select(Product).where(Product.id == product_id))
    if not prod:
        raise HTTPException(status_code=404, detail="Product not found")

    for field, val in payload.model_dump(exclude_unset=True).items():
        setattr(prod, field, val)

    db.commit()
    db.refresh(prod)
    return prod
