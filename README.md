# SmartTech AI Customer Support Email Agent

SmartTech is a full-stack support application for turning inbound customer email into traceable, evidence-grounded response drafts. It combines a FastAPI API and employee dashboard with PostgreSQL, Gmail, Groq, and a pgvector-backed knowledge base.

The application is designed to keep a support employee in the loop. With the default configuration, generated replies are saved for review; they are not sent automatically.

## Problem

Support teams need to understand a customer's issue, find accurate product or policy information, check relevant customer records, and respond consistently. This project brings those records and steps into one workflow while keeping the response linked to its source evidence and review history.

## Features

- Authenticated employee dashboard and role-based API access.
- Customer, product, order, device, warranty, ticket, and email records.
- Gmail OAuth connection, inbox synchronization, thread-aware ticketing, and reply sending.
- Groq-backed response generation through LangChain.
- Document ingestion and semantic retrieval using Hugging Face embeddings and PostgreSQL/pgvector.
- Response quality, grounding, privacy, and inbound email risk checks.
- Persistent response drafts with review, approval, rejection, edit invalidation, and audited sending.
- Optional automatic dispatch guarded by administrator configuration, Gmail verification, confidence, and safety checks. It is disabled by default.
- Dashboard metrics, system health, execution history, audit events, and optional Langfuse tracing.
- CSV/XLSX customer import preview and confirmation.

## Architecture

The employee UI is plain HTML, CSS, and JavaScript. FastAPI serves it from `/app/` and exposes the API. PostgreSQL is the system of record; pgvector stores knowledge embeddings. The Gmail polling worker runs in the FastAPI process.

```mermaid
flowchart LR
    Gmail[Gmail inbox] -->|OAuth + History API polling| Sync[Sync worker]
    UI[Employee dashboard] --> API[FastAPI API]
    Sync --> DB[(PostgreSQL + pgvector)]
    API --> DB
    API --> Workflow[Support workflow]
    Sync --> Workflow
    Workflow --> Retrieval[Product-scoped knowledge retrieval]
    Retrieval --> DB
    Workflow --> Groq[Groq chat model]
    Workflow --> Checks[Grounding, quality, privacy, and risk checks]
    Checks --> Draft[Persist response draft]
    Draft --> Review[Human review and approval]
    Review -->|Approved, unchanged draft| Gmail
    Workflow -. optional traces .-> Langfuse[Langfuse]
```

### Inbound support workflow

1. The Gmail worker checks for a valid authorization with the required `gmail.modify` and `gmail.send` scopes. It uses Gmail History API changes and a durable history cursor to find new Inbox messages.
2. The worker ignores spam/trash and self-sent messages, deduplicates provider message IDs, matches a customer by normalized sender address, and creates or reuses a ticket for the same customer and Gmail thread.
3. The workflow collects ticket and customer context and searches indexed support documents. Retrieval can be scoped to a product and includes source citations.
4. LangChain composes the prompt and Groq generation call. Follow-up checks evaluate grounding, response quality, safety, privacy, sentiment, and whether the case needs human review.
5. The workflow stores a draft and execution record in PostgreSQL. Failed processing is recorded and can be retried by the worker; it does not count as successfully processed email.
6. An authorized employee reviews the draft. A reviewer can edit, approve, or reject it. Sending checks the approval and message integrity immediately before using Gmail.

The ticket API can also run the support workflow manually. If a ticket has no associated email, the agent route falls back to the separate `AgentOrchestrator` path.

### RAG implementation

Knowledge files are uploaded through the knowledge API. The current parser accepts PDF, TXT, and Markdown files up to 10 MB. Text is split into overlapping chunks, embedded with the configured Sentence Transformers model, and stored with source metadata in PostgreSQL using pgvector. Retrieval embeds the question with the same model and runs cosine-distance search, with product/model compatibility filters and citations. The active retrieval implementation uses SQLAlchemy and pgvector SQL; it does not use LangChain's PGVector vector-store integration.

The default embedding model is `sentence-transformers/all-MiniLM-L6-v2` with 384 dimensions. The model may be downloaded the first time it is needed. Retrieved text is evidence for generation; the quality and policy checks can still require escalation or review.

### AI workflow

The email workflow is a Python orchestration service using LangChain prompt/model/parser chains for generation and selected quality checks. Groq is accessed through a shared LangChain `ChatOpenAI` client configured with the Groq API base URL. The configured model is checked against the provider's model list. This email path uses explicit application workflow steps and SQL-backed context; it is not a free-running ReAct tool-selection loop. The alternate `AgentOrchestrator`/ReAct implementation is used by the manual agent route when the ticket has no email record.

## Human review and sending

`AUTO_SEND_ENABLED` defaults to `false`, and the demo seed creates the persistent setting as false when it is not already present. In this mode, generated messages remain `pending_review` until an authorized manager or administrator approves them. An edit clears the approval and returns the draft to review. The send endpoint verifies the approved body hash and recipient before dispatching through Gmail, then records the result and audit event.

An administrator can enable optional automatic dispatch in settings only after the Gmail identity, API, and send permission have been verified. Automatic dispatch still requires the response to pass the workflow's eligibility checks, including confidence, grounding, safety, and human-review requirements. Check the effective database and environment setting before relying on a deployment's behavior.

## Technology

- Python 3.10+ and FastAPI
- SQLAlchemy 2, Alembic, PostgreSQL, and pgvector
- LangChain and Groq's OpenAI-compatible API
- Sentence Transformers for embeddings
- Google Gmail API and OAuth 2.0
- Plain HTML, CSS, and JavaScript dashboard served by FastAPI
- pytest for backend tests; optional Langfuse tracing

## Requirements

- Python 3.10 or newer (Python 3.11 is used by the project environment).
- PostgreSQL with the pgvector extension available to the database user.
- A Groq API key for live response generation.
- Gmail OAuth client credentials and a user-granted Gmail authorization for live synchronization and sending. Gmail is optional for local API development.
- Outbound HTTPS access to the configured providers. The embedding model may require a first-time download unless already cached.
- Node.js is not required: the current frontend has no package manager, bundler, or separate frontend server.

## Configure the environment

From the repository root, copy the example and edit `backend/.env`:

```powershell
Copy-Item backend/.env.example backend/.env
```

Set at least a real `DATABASE_URL` and a long random `JWT_SECRET` before starting. Set `GROQ_API_KEY` to enable live generation. Keep `backend/.env`, OAuth files, tokens, and database exports private; do not paste their values into issues, logs, or documentation.

Important variables are listed below. Defaults are in [`backend/.env.example`](backend/.env.example) and [`backend/app/core/config.py`](backend/app/core/config.py).

| Variable | Purpose |
| --- | --- |
| `APP_ENV` | Runtime mode; development mode allows permissive CORS for local development. |
| `DATABASE_URL` | PostgreSQL SQLAlchemy URL, for example `postgresql+psycopg://user:password@localhost:5432/smarttech`. |
| `JWT_SECRET`, `JWT_ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES` | Signing and lifetime for employee access tokens. Use a private random secret outside local development. |
| `GROQ_API_KEY`, `GROQ_MODEL`, `GROQ_BASE_URL` | Groq credentials and model endpoint. |
| `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION` | Embedding backend/model and vector size. |
| `RAG_CHUNK_SIZE`, `RAG_CHUNK_OVERLAP`, `RAG_TOP_K`, `RAG_PRODUCT_TOP_K` | Document chunking and retrieval limits. |
| `GMAIL_CREDENTIALS_FILE`, `GMAIL_TOKEN_FILE`, `GMAIL_SCOPES`, `GMAIL_REDIRECT_URI` | OAuth client, private token path, requested scopes, and callback URI. |
| `GMAIL_POLL_INTERVAL_SECONDS` | Gmail worker polling interval. |
| `AUTO_SEND_ENABLED`, `CONFIDENCE_THRESHOLD` | Default auto-dispatch policy and confidence threshold. The persisted database setting can also affect the effective behavior. |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | Optional tracing configuration. |
| `CORS_ORIGINS` | Comma-separated allowed origins outside development mode. |
| `UPLOAD_DIR`, `SAMPLE_KNOWLEDGE_DIR`, `KNOWLEDGE_BASE_DIR` | Local document/upload directories. Do not commit customer uploads. |

### Gmail setup

Create a Google OAuth client, enable the Gmail API for its Cloud project, and configure the consent screen/test users as needed. Keep the client JSON and token outside source control (the default paths under `backend/secrets/` are ignored). Connect through the dashboard or the Gmail authorization endpoint. The configured callback must match the OAuth client setup. The application verifies the Gmail profile and actual granted scopes; a saved token alone is not treated as a verified connection. Google may require additional consent-screen review before a production rollout.

## Install, migrate, and run

The included PowerShell scripts are the supported local workflow on Windows. Run from the repository root:

```powershell
.\scripts\setup.ps1
```

Edit `backend/.env` and make sure PostgreSQL is running with pgvector enabled. Apply migrations explicitly:

```powershell
.\scripts\migrate.ps1
```

For a local demo only, populate sample users and records:

```powershell
.\scripts\seed.ps1
```

For a fresh installation, either create the first administrator with `POST /api/auth/bootstrap-admin` or configure `SEED_ADMIN_PASSWORD` privately before running the seed command. The seeder fails before writing if it needs to create the administrator and the password is missing or shorter than 12 characters. It never changes an existing account password. Optional demo accounts are disabled by default; set `SEED_DEMO_USERS=true` and provide private `SEED_MANAGER_PASSWORD`, `SEED_AGENT_PASSWORD`, and `SEED_ANALYST_PASSWORD` values to create them. Never place real values in `.env.example` or publish your `.env` file. Bootstrap is serialized in PostgreSQL and becomes unavailable after the first user is created.

Sample manuals are indexed separately from the account/demo seed. After starting the API and signing in as a manager or administrator, call `POST /api/knowledge/seed-sample-docs` once (also available in `/docs`). Until approved knowledge documents are indexed, product-specific retrieval intentionally returns no manual results and technical replies remain subject to review.

Start the backend and dashboard:

```powershell
.\start-smarttech.bat
```

The launcher validates dependencies, database readiness, and migration revision, then runs Uvicorn on `http://127.0.0.1:8000`. It does not automatically apply migrations or seed data. The dashboard is served at `http://127.0.0.1:8000/app/`; API docs are at `/docs`, liveness at `/health`, and dependency readiness at `/ready`. Stop the managed process with `.stop-smarttech.bat`.

The frontend has no separate build or run command. FastAPI serves `frontend/` as static files.

## Database and persistence

Alembic migrations are in `backend/alembic/versions/`. They define the PostgreSQL schema and pgvector extension setup. Startup checks the schema revision but leaves migrations to the explicit migration command. The main persisted records cover users, customers, products, orders/items, devices, warranties, tickets/messages, Gmail messages, response drafts, knowledge documents/chunks, agent/tool executions, audit logs, system settings, and dashboard snapshots.

Database startup does not reset or seed existing records. The optional seeder skips existing entities and records one seed audit event, so rerunning it does not duplicate the seed dataset or overwrite account passwords. Configure the administrator password privately before seeding a new database. Back up the database before applying schema changes or importing real customer data.

## Project layout

```text
backend/
  app/                 FastAPI app, API routers, models, schemas, services, integrations
  alembic/             Database migration environment and revisions
  tests/               Backend unit and integration-style tests
  .env.example         Placeholder configuration template
  requirements.txt     Python dependencies
data/
  sample_knowledge_base/  Example support documents
  sample_import/          Example customer import templates
  uploads/                Two synthetic PDF fixtures used by regression tests; other uploads are ignored
docs/                  Implementation and workflow notes
frontend/              Static employee dashboard
scripts/               Setup, migration, seed, verification, and lifecycle scripts
README.md
```

Local runtime uploads, database dumps, OAuth material, virtual environments, logs, browser profiles, and editor metadata are excluded from Git.

## Tests and checks

The test suite is backend-only. External providers and the Gmail worker are replaced with local doubles by default. Several API integration tests use `DATABASE_URL` directly and create/clean records, so run the full suite only against a dedicated disposable test database with the expected demo fixtures. The repository does not create an isolated database automatically. From the repository root:

```powershell
.\scripts\verify.ps1
```

Or run directly:

```powershell
backend\.venv\Scripts\python.exe -m pytest -q
backend\.venv\Scripts\python.exe -m compileall -q backend\app backend\alembic
```

The optional live Groq integration test is skipped by default. To opt in, set `RUN_LIVE_GROQ_TESTS=1` in the environment and run the integration test with valid Groq credentials. It makes a real provider request. There is no frontend test or bundler configuration; if Node.js is installed, `node --check frontend/js/app.js` and `node --check frontend/js/login.js` provide syntax checks only.

## Security

- Never commit `.env`, Gmail OAuth client/token JSON, private keys, access tokens, production database dumps, customer exports, or uploaded customer documents.
- Use a unique strong `JWT_SECRET`, restrict database network access, and protect PostgreSQL backups and logs as customer data.
- Keep OAuth scopes and API credentials limited to the deployment's needs. Revoke credentials that may have been exposed.
- Treat email bodies and uploaded knowledge files as untrusted input. The workflow includes privacy, grounding, and risk checks, but these are not a substitute for operational review.
- Automatic dispatch is optional and disabled in the example/default configuration. Verify the persisted setting, Gmail connection, and monitoring before any deployment enables it.
- Demo seed accounts are optional and require privately configured passwords; keep those credentials and the seeded database protected.

## Current limitations

- Live generation and Gmail synchronization require provider credentials, network access, and provider-side configuration; offline tests do not prove a deployment's live credentials.
- Gmail OAuth consent-screen verification and Gmail API enablement are managed in Google Cloud, outside this application.
- The Gmail polling worker runs inside each API process. A multi-process or horizontally scaled deployment needs a single-worker/leader strategy to avoid redundant polling.
- Knowledge ingestion supports text-extractable PDF, TXT, and Markdown; scanned PDFs need OCR support that is not implemented here.
- The UI is a static JavaScript application with no frontend automated test suite or production bundler in this repository.
- Deployment packaging, CI/CD, and production infrastructure hardening are not included.
- Dashboard delivery statistics are derived from current records and audit data; there is no complete immutable ledger for every delivery attempt.

## Future improvements

- Replace fixed demo passwords with per-installation credentials and a first-run setup flow.
- Add a coordinated background-worker deployment model for multiple API instances.
- Add OCR and broader document-format support, plus more retrieval evaluations.
- Add frontend automated tests and a production asset pipeline.
- Add CI checks for migrations, dependency vulnerabilities, secret scanning, and full-stack integration.
- Add immutable delivery-attempt records and deployment-focused operational metrics.

## Further documentation

See [`docs/PROJECT_WORKFLOW.md`](docs/PROJECT_WORKFLOW.md) for implementation details on synchronization, retrieval, approval state, and persisted reporting.
