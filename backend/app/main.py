from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from app.core.config import settings
from app.db.session import engine

app = FastAPI(title="SmartTech Support Agent")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins.split(","),
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health():
    return {"status": "ok", "auto_send_enabled": settings.auto_send_enabled}


@app.get("/ready")
def ready():
    try:
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        return {"database": "ok"}
    except Exception as e:  # real connection test, never assumed
        return {"database": "error", "detail": type(e).__name__}
