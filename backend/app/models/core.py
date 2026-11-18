"""Backwards-compatibility re-exports for core models."""
from app.db.base import Base
from app.models.email import EmailMessage
from app.models.draft import Draft

__all__ = ["Base", "EmailMessage", "Draft"]
