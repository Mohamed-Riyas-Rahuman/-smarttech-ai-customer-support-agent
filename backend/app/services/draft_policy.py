"""Pure approval/send rules (no DB) so they can be tested in isolation."""
import hashlib
from dataclasses import dataclass, replace

ROLES_CAN_APPROVE = {"admin", "reviewer"}


class PolicyError(Exception):
    pass


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


@dataclass(frozen=True)
class DraftState:
    body: str
    status: str = "pending_review"  # pending_review|approved|rejected|sent|send_ambiguous
    approved_by: int | None = None
    approved_hash: str | None = None


def approve(d: DraftState, user_id: int, role: str) -> DraftState:
    if role not in ROLES_CAN_APPROVE:
        raise PolicyError("role not permitted to approve")
    if d.status != "pending_review":
        raise PolicyError(f"cannot approve draft in status {d.status}")
    return replace(d, status="approved", approved_by=user_id, approved_hash=body_hash(d.body))


def edit(d: DraftState, new_body: str) -> DraftState:
    """Any edit invalidates a previous approval."""
    if d.status in {"sent", "send_ambiguous"}:
        raise PolicyError("cannot edit a draft that was sent")
    return replace(d, body=new_body, status="pending_review", approved_by=None, approved_hash=None)


def assert_can_send(d: DraftState) -> None:
    """Independent backend check run immediately before any send."""
    if d.status == "sent":
        raise PolicyError("draft already sent")
    if d.status == "send_ambiguous":
        raise PolicyError("previous send outcome unknown; reconcile before retrying")
    if d.status != "approved" or d.approved_by is None:
        raise PolicyError("draft is not approved")
    if d.approved_hash != body_hash(d.body):
        raise PolicyError("draft changed after approval")
