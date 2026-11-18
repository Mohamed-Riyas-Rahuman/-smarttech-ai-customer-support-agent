"""Main FastAPI application for SmartTech Support Agent."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from app.core.config import settings
from app.core.logging import logger
from app.db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle for background services."""
    # Start Gmail background sync worker
    try:
        from app.services.sync_worker import start_worker
        start_worker()
        logger.info("Gmail sync worker started via FastAPI lifespan.")
    except Exception as e:
        logger.warning("Could not start Gmail sync worker (%s).", type(e).__name__)

    yield  # Application runs here

    # Stop Gmail background sync worker
    try:
        from app.services.sync_worker import stop_worker
        stop_worker()
        logger.info("Gmail sync worker stopped via FastAPI lifespan.")
    except Exception as e:
        logger.warning("Error stopping Gmail sync worker (%s).", type(e).__name__)


app = FastAPI(
    title="SmartTech Support Agent API",
    description="Enterprise AI-Powered Customer Support Platform for SmartTech Electronics",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

origins = [orig.strip() for orig in settings.cors_origins.split(",") if orig.strip()]
# In development, allow all origins to support opening index.html directly from filesystem
if settings.app_env == "development":
    origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True if origins != ["*"] else False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/app/", status_code=307)

@app.get("/health", tags=["General"])
def health():
    from app.db.session import SessionLocal
    from app.services.auto_send import is_auto_send_enabled
    db = SessionLocal()
    try:
        auto_send_enabled = is_auto_send_enabled(db)
    finally:
        db.close()
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "auto_send_enabled": auto_send_enabled,
        "environment": settings.app_env,
    }


@app.get("/ready", tags=["General"])
def ready():
    status = {"database": "error", "pgvector": "unknown", "groq": "not_configured", "gmail": "not_configured"}

    # Database check
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            status["database"] = "ok"
            vector_version = conn.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar()
            status["pgvector"] = f"v{vector_version}" if vector_version else "not_installed"
    except Exception as e:
        logger.error(f"Readiness DB check failed: {type(e).__name__}")
        status["database"] = f"error: {type(e).__name__}"

    # Verify Groq key and configured model without exposing credentials.
    from app.integrations.llm.llm_service import groq_model_status
    status["groq"] = groq_model_status()["status"]
    status["groq_model"] = settings.groq_model

    # Gmail status check
    from app.services.integrations.gmail import gmail_service
    gmail_status = gmail_service.get_authorization_status()
    status["gmail"] = gmail_status.get("status", "authorization_required")

    # Overall readiness: database is required; optional integrations can be not_configured
    is_ready = status["database"] == "ok" and status["pgvector"] not in {"unknown", "not_installed"}
    return {
        "ready": is_ready,
        "dependencies": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# Include Routers
from app.api.routers import agent, auth, customers, dashboard, devices, drafts, gmail, knowledge, orders, products, settings as settings_router, tickets, warranties

app.include_router(auth.router)
app.include_router(customers.router)
app.include_router(products.router)
app.include_router(orders.router)
app.include_router(devices.router)
app.include_router(warranties.router)
app.include_router(tickets.router)
app.include_router(dashboard.router)
app.include_router(settings_router.router)
app.include_router(knowledge.router)
app.include_router(agent.router)
app.include_router(drafts.router)
app.include_router(gmail.router)

# Static frontend files for employee dashboard
from fastapi.staticfiles import StaticFiles
from app.core.config import WORKSPACE_ROOT
frontend_dir = WORKSPACE_ROOT / "frontend"
if frontend_dir.exists():
    app.mount("/app", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
