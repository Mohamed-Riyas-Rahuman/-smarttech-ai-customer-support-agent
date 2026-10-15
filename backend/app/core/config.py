"""Central settings. Paths resolve relative to backend/, not the CWD."""
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")
    app_env: str = "development"
    database_url: str = "postgresql+psycopg://postgres:CHANGE_ME@localhost:5432/customer_support_db"
    jwt_secret: str = "CHANGE_ME"
    openrouter_api_key: str = ""
    openrouter_model: str = "openai/gpt-4o-mini"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    auto_send_enabled: bool = False
    cors_origins: str = "http://localhost:5173"


settings = Settings()
