# Project Status (honest report)

Environment used to build: Linux sandbox, Python 3.12, no network, no PostgreSQL,
no pip packages (fastapi, sqlalchemy, pytest not installable).

## Implemented and tested
- Draft approval/send policy (backend/app/services/draft_policy.py): unapproved
  drafts cannot send; only admin/reviewer can approve; editing invalidates approval;
  tampered body blocked; no resend after sent/ambiguous. 6 unittest tests PASSED.

## Implemented, NOT run
- FastAPI app with /health and /ready (needs fastapi installed; byte-compiled only)
- Settings (pydantic-settings), SQLAlchemy models for email_messages (unique provider
  ID for dedup) and drafts. Not executed against PostgreSQL.
- scripts/setup.ps1, start.ps1, verify.ps1 - never run on Windows.

## NOT implemented
Alembic migrations, auth/RBAC, Gmail OAuth/sync/send, OpenRouter client, RAG and
embeddings, agent (planning, skills, tools, guardrails, tracing), background worker,
evaluation, all 13 frontend pages, demo data, most tests, stop/reset scripts,
docs/ guides. Auto-send is false by default in config.
