"""Component 6: Conversation Memory - Context preservation across email threads and ticket history."""
import re
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models.ticket import SupportTicket, TicketMessage


class ConversationMemory:
    def __init__(self, db: Session):
        self.db = db

    def get_conversation_history(self, ticket_id: int) -> list[dict[str, Any]]:
        """Retrieve chronological message history for a ticket without leaking sensitive secrets."""
        messages = self.db.scalars(
            select(TicketMessage)
            .where(TicketMessage.ticket_id == ticket_id)
            .order_by(TicketMessage.created_at.asc())
        ).all()

        history = []
        for m in messages:
            clean_body = self.sanitize_content(m.message_body)
            history.append({
                "id": m.id,
                "sender_type": m.sender_type,
                "sender_name": m.sender_name or m.sender_type.capitalize(),
                "sender_email": m.sender_email,
                "body": clean_body,
                "is_internal": m.is_internal_note,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            })
        return history

    def format_thread_context_for_prompt(self, ticket_id: int, max_turns: int = 6) -> str:
        """Format recent customer and agent turns into prompt grounding context."""
        history = self.get_conversation_history(ticket_id)
        if not history:
            return "No previous conversation history on file."

        turns = history[-max_turns:]
        lines = ["=== TICKET CONVERSATION HISTORY ==="]
        for t in turns:
            sender = "Customer" if t["sender_type"] == "customer" else "Support Agent"
            lines.append(f"[{sender} ({t['sender_name']})]:\n{t['body']}\n")

        return "\n".join(lines)

    @staticmethod
    def sanitize_content(text: str) -> str:
        """Sanitize potentially sensitive payment info or passwords from memory retention."""
        if not text:
            return ""
        # Mask 16-digit credit card patterns
        sanitized = re.sub(r"\b(?:\d{4}[-\s]?){3}\d{4}\b", "[CARD REDACTED]", text)
        # Mask password exposures
        sanitized = re.sub(r"(?i)(password|passcode|secret)[\s:=]+([^\s,;]+)", r"\1: [REDACTED]", sanitized)
        return sanitized
