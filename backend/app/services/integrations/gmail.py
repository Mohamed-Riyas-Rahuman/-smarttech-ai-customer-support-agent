"""Re-export from app.integrations.email.gmail_service for backward compatibility."""
from app.integrations.email.gmail_service import GmailService, gmail_service

__all__ = ["GmailService", "gmail_service"]
