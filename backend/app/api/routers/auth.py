"""Authentication and User Management router."""
from datetime import timedelta
from collections import defaultdict, deque
from time import monotonic
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from app.api.dependencies import get_current_user, get_db, require_admin
from app.core.config import settings
from app.core.security import create_access_token, hash_password, verify_password
from app.models.user import User
from app.models.audit import AuditLog
from app.schemas.auth import LoginRequest, TokenResponse, UserCreate, UserOut, UserUpdate

router = APIRouter(prefix="/api/auth", tags=["Authentication"])

_login_failures: dict[str, deque[float]] = defaultdict(deque)
_LOGIN_WINDOW_SECONDS = 900
_LOGIN_MAX_FAILURES = 8


@router.get("/login-users")
def list_login_users(db: Session = Depends(get_db)):
    """Return only active account emails needed by the public sign-in chooser."""
    users = db.scalars(select(User.email).where(User.is_active.is_(True)).order_by(User.email)).all()
    return [{"email": email} for email in users]


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    """Authenticate user with bcrypt and issue a signed HttpOnly session cookie."""
    now = monotonic()
    key = f"{request.client.host if request.client else 'unknown'}:{payload.email.strip().lower()}"
    failures = _login_failures[key]
    while failures and now - failures[0] > _LOGIN_WINDOW_SECONDS:
        failures.popleft()
    if len(failures) >= _LOGIN_MAX_FAILURES:
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in 15 minutes.", headers={"Retry-After": "900"})
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not verify_password(payload.password, user.hashed_password):
        failures.append(now)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is inactive. Please contact your administrator.",
        )

    _login_failures.pop(key, None)
    token = create_access_token(
        data={"sub": str(user.id), "email": user.email, "role": user.role},
        expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
    )

    # Audit log login event
    log = AuditLog(
        user_id=user.id,
        action="login_success",
        entity_type="user",
        entity_id=str(user.id),
        details=f"User {user.email} logged in successfully.",
    )
    db.add(log)
    db.commit()
    response.set_cookie("st_session", token, httponly=True, secure=settings.app_env != "development", samesite="strict", max_age=settings.access_token_expire_minutes * 60, path="/")
    return {"expires_in": settings.access_token_expire_minutes * 60, "user": UserOut.model_validate(user)}


@router.get("/me", response_model=UserOut)
def get_me(current_user: User = Depends(get_current_user)):
    """Retrieve profile of the currently authenticated user."""
    return current_user


@router.post("/logout")
def logout(response: Response, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Logout endpoint recording audit event."""
    log = AuditLog(
        user_id=current_user.id,
        action="logout",
        entity_type="user",
        entity_id=str(current_user.id),
        details=f"User {current_user.email} logged out.",
    )
    db.add(log)
    db.commit()
    response.delete_cookie("st_session", path="/", httponly=True, secure=settings.app_env != "development", samesite="strict")
    return {"message": "Logged out successfully"}


@router.post("/bootstrap-admin", response_model=UserOut)
def bootstrap_admin(payload: UserCreate, db: Session = Depends(get_db)):
    """Create exactly one initial administrator when the database is empty."""
    try:
        # A transaction-scoped PostgreSQL lock closes the count-then-create race
        # across concurrent requests and application workers.
        db.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": 721946380214})
        user_count = db.scalar(select(func.count(User.id)))
        if user_count > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Administrator bootstrap is disabled because users already exist in the database.",
            )

        admin = User(
            email=payload.email.lower(),
            full_name=payload.full_name,
            hashed_password=hash_password(payload.password),
            role="admin",
            is_active=True,
        )
        db.add(admin)
        db.flush()
        db.add(AuditLog(
            user_id=admin.id,
            action="bootstrap_admin",
            entity_type="user",
            entity_id=str(admin.id),
            details=f"Initial administrator {admin.email} created via bootstrap.",
        ))
        db.commit()
        db.refresh(admin)
        return admin
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    """List all system users (Administrator only)."""
    return db.scalars(select(User).order_by(User.id)).all()


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Create a new user with designated role (Administrator only)."""
    existing = db.scalar(select(User).where(User.email == payload.email.lower()))
    if existing:
        raise HTTPException(status_code=400, detail="User with this email already exists.")

    new_user = User(
        email=payload.email.lower(),
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=payload.role,
        is_active=True,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    db.add(AuditLog(
        user_id=current_user.id,
        action="create_user",
        entity_type="user",
        entity_id=str(new_user.id),
        details=f"User {new_user.email} ({new_user.role}) created by admin {current_user.email}.",
    ))
    db.commit()

    return new_user


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    payload: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Update user properties or role (Administrator only)."""
    target = db.scalar(select(User).where(User.id == user_id))
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    if payload.full_name is not None:
        target.full_name = payload.full_name
    if payload.role is not None:
        target.role = payload.role
    if payload.is_active is not None:
        target.is_active = payload.is_active
    if payload.password is not None:
        target.hashed_password = hash_password(payload.password)

    db.commit()
    db.refresh(target)

    db.add(AuditLog(
        user_id=current_user.id,
        action="update_user",
        entity_type="user",
        entity_id=str(target.id),
        details=f"User {target.email} updated by admin {current_user.email}.",
    ))
    db.commit()

    return target
