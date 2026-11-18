# SmartTech Support Agent: Project Workflow

This document describes the implementation in this repository. PostgreSQL is the application database and pgvector stores knowledge embeddings. Existing customer, ticket, message, draft, and knowledge rows are not reset by application startup or by the dashboard snapshot migration.

## Architecture

- **Frontend:** `frontend/index.html` is a plain JavaScript single-page employee dashboard served by FastAPI at `/app/`. It calls authenticated `/api/*` routes with a bearer token.
- **API and access control:** `backend/app/main.py` registers routers and starts/stops the Gmail worker through FastAPI lifespan. `backend/app/api/dependencies.py` loads active users from JWTs and applies role checks.
- **Persistence:** SQLAlchemy models in `backend/app/models/` use the PostgreSQL URL from `DATABASE_URL`. Alembic migrations live in `backend/alembic/versions/`.
- **Knowledge search:** `backend/app/services/rag/` parses/chunks documents, builds configured Hugging Face embeddings, stores vectors in `knowledge_chunks`, and searches PostgreSQL/pgvector with cosine distance.
- **LLM:** `backend/app/integrations/llm/llm_service.py` is the shared LangChain `ChatOpenAI` factory for Groq. It checks the configured model through Groq's Models API before generation. No API key is written to traces.
- **LangChain:** LangChain is used for selected, active workflow components: `ChatPromptTemplate`, `ChatOpenAI`, prompt/model/parser `Runnable` composition, and quality-check chains. The email workflow is custom Python orchestration around those chains and custom SQL retrieval. The agent API uses this workflow for tickets with an email record and falls back to `AgentOrchestrator`/`react_agent.py` for tickets without one.
- **Langfuse:** `LangChainSupportWorkflow` emits custom named workflow spans and passes `PrivacySafeLangfuseCallbackHandler` to LangChain chains when configured. `AgentExecutionTracer` also contains Langfuse callbacks for the dormant orchestrator path. The approval API emits low-PII `draft_approved`, `draft_rejected`, and `approved_draft_sent` events with actor role only; PostgreSQL `audit_logs` remains the detailed audit source. Trace failures are guarded so they do not stop support processing.
- **Gmail:** `backend/app/integrations/email/gmail_service.py` handles OAuth credentials, Gmail API clients, message parsing, and reply delivery. `backend/app/services/sync_worker.py` runs one polling thread per application process.

## Incoming email workflow

1. FastAPI lifespan starts `sync_worker.start_worker()`; the worker requires valid Gmail credentials and actual `gmail.modify` and `gmail.send` grants before it polls.
2. It verifies the Gmail profile and uses Gmail History API `messageAdded` events after a durable history cursor, then loads only messages added to Inbox. A missing cursor establishes a baseline without importing old mail; an expired cursor is safely rebased. Spam/trash and self-sent mail are ignored, and provider IDs are protected by a database unique constraint plus a pre-insert lookup.
3. `sync_worker._ingest_message` matches a customer by exact normalized sender email, creates a customer when needed, and reuses a thread ticket only if it belongs to the same customer. It writes the customer `TicketMessage` and `EmailMessage`.
4. `LangChainSupportWorkflow.run_for_ticket` calls `EmailService.extract_context_for_ticket`, performs product-scoped pgvector retrieval, builds the support prompt, invokes the shared Groq-backed LangChain generation chain, and runs sentiment/safety/grounding chains.
5. `DraftService` persists the generated answer as `pending_review` while the database auto-send flag is off. The email is marked processed only after the workflow returns successfully. Incomplete old rows marked unprocessed can be retried on later polling.
6. `workflow_audit.record_workflow_execution` persists completed/failed workflow attempts in `agent_executions`. No Langfuse count is used as a dashboard metric.
7. A manager/admin approves or rejects through `/api/drafts`; sending is separately authorized through the approved-draft send endpoint. Approval binds a hash to the approved body. Editing clears approval. Manual send remains available after explicit approval even when automatic dispatch is disabled.

## RAG workflow

Knowledge files are uploaded/indexed through `backend/app/api/routers/knowledge.py`. The RAG service parses supported files, chunks text using configured chunk size/overlap, embeds with the configured Hugging Face model and dimension, then saves source metadata and vectors to PostgreSQL. Retrieval embeds the query with the same model, filters incompatible model/dimension rows, executes cosine-distance search, and returns source citations. `LangChainSupportWorkflow` places retrieved passages and citations into the prompt. The active workflow uses custom SQLAlchemy/pgvector retrieval; it does not use LangChain's PGVector vector-store class.

## AI workflow and tools

- Main HTTP entry: `POST /api/agent/run` in `backend/app/api/routers/agent.py` checks role access and calls `LangChainSupportWorkflow.run_for_ticket`.
- Gmail entry: the Gmail router and background worker call the same workflow for incoming mail.
- Main classification/LLM: prompt templates in `services/langchain_workflow/prompts.py`, `get_response_generation_chain` in `chains.py`, and `get_llm` in `integrations/llm/llm_service.py` form the active LangChain chain.
- Quality checks: `QualityChecker` uses LangChain prompt/model/parser chains. Python code sequences retrieval, prompting, generation, checks, ticket updates, and draft persistence.
- Business data tools: `services/agents/tools.py` contains SQLAlchemy operations. `services/agents/langchain_tools.py` adapts some of them to LangChain tools for `react_agent.py`; the `AgentOrchestrator` that calls this path currently has no route caller. Thus the active email workflow has no dynamic tool-selection loop.
- Customer access: authenticated routes enforce user roles; customer matching is exact by sender address. Tool adapters bind to a ticket/customer execution context and check ownership.
- Errors: workflow errors are returned to the caller, traced with a safe error type, and recorded as failed `AgentExecution` rows for the route/worker paths.

## Gmail OAuth and synchronization

The app reads the OAuth client JSON at `GMAIL_CREDENTIALS_FILE` and authorized-user token at `GMAIL_TOKEN_FILE`; neither value belongs in source control. The current installed Desktop OAuth client can be retained for local loopback use. The configured `GMAIL_REDIRECT_URI` defaults to `http://localhost:8000/api/gmail/oauth/callback`; the Settings button starts OAuth and the Google redirect reaches the backend callback. State is signed with the application secret, expires after ten minutes, and is consumed once. The short-lived PKCE verifier is stored in a secret system-setting row and redacted from settings APIs. The callback validates state, exchanges the code with the original verifier, atomically saves credentials, then verifies the Gmail profile and actual granted scopes. Token refresh is serialized across API and worker threads in the process; revoked/missing grants and OAuth client errors are surfaced as sanitized status codes.

The worker shares the same `gmail_service` credential loader as API calls. Each successful poll updates last-success/checkpoint settings and operational metrics. Unauthorized credentials do not count as a successful poll; the UI reports authorization required or a paused worker instead of claiming monitoring is active. Historical email row totals are not evidence of current Gmail connectivity.

Google Cloud Console cannot be inspected or modified by this app. If authorization still fails, confirm **Gmail API is enabled** in the OAuth client's Google Cloud project and the consent screen is configured for the account. For an External app left in Testing, add the support mailbox as a test user; Gmail scopes are sensitive/restricted and may require OAuth verification for production use. For Desktop OAuth, use Google's supported localhost/loopback redirect flow and keep the redirect URI consistent with this app. The application cannot prove the Cloud project toggle or consent-screen audience without a successful live Google request.

## Dashboard and historical reports

`GET /api/dashboard/stats` calculates live values from PostgreSQL on every request. Volume counters cover persisted history; current ticket/draft state metrics exclude resolved/closed tickets where the metric means current attention. Incoming/processed emails are rows in `email_messages`; processed means `is_processed` and linked to a ticket. AI successes count distinct support tickets with a completed workflow; failures count failed execution attempts. Pending responses count drafts in `pending_review`; awaiting approval counts distinct active tickets with that state or a pending draft. Sent responses count drafts whose current state is `sent`. Failed sends count drafts whose current state records a terminal failure; ambiguous outcomes are excluded because Gmail may have accepted the message. Attention includes active tickets with high/urgent priority, a recommended/actual escalation, an approval state, or a pending draft. High priority counts only the `high` priority value; escalated counts only `escalation_status=escalated` (not merely `recommended`).

The current schema does not have a durable email-delivery-attempt ledger, so `failed_sends` counts current terminal `send_failed` drafts; it cannot count every historical failed attempt if a draft was later retried. Ambiguous sends are excluded because Gmail may have accepted them. Sent responses require both the current `sent` draft state and a successful Gmail-send/auto-send audit action; unverified seeded or simulated `sent` rows are excluded. Legacy email rows were marked processed at ingestion in the previous code, before the AI result; their old `is_processed` values cannot be reconstructed without deleting or reclassifying historical records. New processing now records success only after the workflow completes. The response-time metric is derived from the first customer message and subsequent first agent message; if either is absent, it is excluded and an empty sample returns zero.

`GET /api/dashboard/snapshots` provides up to 52 immutable snapshots and each snapshot's difference from the previous one. Admins can save a manual snapshot through `POST /api/dashboard/snapshots`; duplicate saves in seven days return HTTP 409 unless `force=true` is explicitly used. Dashboard refresh requests use read-only GET endpoints. Snapshots are written only through the explicit administrator POST endpoint.

## Background scheduling and recovery

`main.lifespan` starts one daemon Gmail poller and stops it at shutdown. Polling uses the configured interval and backs off on errors. After OAuth succeeds, the callback wakes the worker immediately. Worker status is available at `GET /api/gmail/worker/status`. The worker advances its Gmail History API cursor only after message processing succeeds; failed message reads retain the prior cursor for a safe retry. Last successful poll, sanitized last error, worker state, and poll metrics are held in `system_settings`.

## Email approval and safety

`auto_send_enabled` is a persistent `system_settings` value and defaults to false; the demo seed also initializes it to false. An admin can enable automatic dispatch only after the Gmail profile, API, and send permission verify successfully. The workflow still requires eligibility checks to pass, including confidence, grounding, safety, and human-review requirements. With auto-send off, incoming replies stay as pending drafts. Approval requires an authorized reviewer/admin and records user plus approved body hash. Only an approved, unchanged draft can be sent through the explicit send endpoint. Do not use the Gmail send endpoint for tests against customers; use mocks.

## Developer setup

PowerShell from the repository root:

```powershell
cd D:\smarttech-support-agent
cd backend
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m alembic upgrade head
cd ..
.\scripts\start.ps1
```

The backend reads variable names from `backend/.env`; provide `DATABASE_URL`, `JWT_SECRET`, `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_BASE_URL`, `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION`, `GMAIL_CREDENTIALS_FILE`, `GMAIL_TOKEN_FILE`, `GMAIL_SCOPES`, `GMAIL_REDIRECT_URI`, `GMAIL_POLL_INTERVAL_SECONDS`, `AUTO_SEND_ENABLED`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_BASE_URL` as needed. Never paste credential values into docs or logs.

Run local checks from `backend`:

```powershell
.venv\Scripts\python.exe -m compileall -q app alembic
.venv\Scripts\python.exe -m pytest -q
```

The app serves the dashboard at `http://localhost:8000/app/`, OpenAPI at `/docs`, health at `/health`, and dependency readiness at `/ready`.

## Project structure notes

Active source is under `backend/app/`, Alembic history under `backend/alembic/`, frontend under `frontend/`, and developer commands under `scripts/`. Compatibility re-exports under `app/services/integrations/gmail.py` and `app/db/` are still imported by callers, so they should not be removed as duplicates. Files with `.bak`/`.before_*` suffixes are retained recovery copies; no source imports them, but they may be useful to recover the user's prior edits and are not runtime dependencies.
