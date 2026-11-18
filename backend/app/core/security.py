"""Security utilities: password hashing, JWT encoding/decoding, role verification."""
from datetime import datetime, timedelta, timezone
from typing import Any
import bcrypt
import jwt
from app.core.config import settings

ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"
ROLE_AGENT = "agent"
ROLE_ANALYST = "analyst"

ALL_ROLES = [ROLE_ADMIN, ROLE_MANAGER, ROLE_AGENT, ROLE_ANALYST]

# Permissions mapping
ROLE_HIERARCHY = {
    ROLE_ADMIN: 4,
    ROLE_MANAGER: 3,
    ROLE_AGENT: 2,
    ROLE_ANALYST: 1,
}


def hash_password(password: str) -> str:
    """Hash a plaintext password using bcrypt."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except Exception:
        return False


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """Generate a signed JWT access token."""
    to_encode = data.copy()
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.access_token_expire_minutes)

    to_encode.update({"exp": expire, "iat": now})
    encoded_jwt = jwt.encode(to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return encoded_jwt


def decode_access_token(token: str) -> dict[str, Any] | None:
    """Validate and decode a JWT token. Returns payload dict or None if invalid/expired."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return payload
    except (jwt.PyJWTError, Exception):
        return None
