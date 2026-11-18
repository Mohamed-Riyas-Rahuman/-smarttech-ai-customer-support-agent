"""Authentication and User Pydantic schemas."""
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    role: str
    is_active: bool
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3)
    password: str = Field(..., min_length=6)


class UserCreate(BaseModel):
    email: str = Field(..., min_length=3)
    full_name: str = Field(..., min_length=2, max_length=255)
    password: str = Field(..., min_length=8)
    role: str = Field("agent", pattern="^(admin|manager|agent|analyst)$")


class UserUpdate(BaseModel):
    full_name: str | None = None
    role: str | None = Field(None, pattern="^(admin|manager|agent|analyst)$")
    is_active: bool | None = None
    password: str | None = Field(None, min_length=8)
