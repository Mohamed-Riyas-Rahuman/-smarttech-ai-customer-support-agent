"""Central settings. Paths resolve relative to backend/, not the CWD."""
from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @field_validator("embedding_dimension")
    @classmethod
    def validate_embedding_dimension(cls, value: int) -> int:
        if not 1 <= value <= 16000:
            raise ValueError("EMBEDDING_DIMENSION must be between 1 and 16000 for pgvector.")
        return value

    gmail_poll_interval_seconds: int = 10

    app_env: str = "development"
    database_url: str = "postgresql+psycopg://USERNAME:PASSWORD@HOST:5432/DATABASE_NAME"

    # JWT Authentication
    jwt_secret: str = "CHANGE_ME_LONG_RANDOM_STRING"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440  # 24 hours

    # Seed credentials are supplied by the installer and never included in repr/logs.
    seed_admin_password: str = Field(default="", repr=False)
    seed_demo_users: bool = False
    seed_manager_password: str = Field(default="", repr=False)
    seed_agent_password: str = Field(default="", repr=False)
    seed_analyst_password: str = Field(default="", repr=False)

    # Groq LLM. The API key is loaded from GROQ_API_KEY and is never exposed.
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # Embeddings
    embedding_provider: str = "huggingface"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dimension: int = 384
    rag_chunk_size: int = 500
    rag_chunk_overlap: int = 50
    rag_top_k: int = 4
    rag_product_top_k: int = 10

    # Gmail Integration
    gmail_credentials_file: str = "secrets/gmail_credentials.json"
    gmail_token_file: str = "secrets/gmail_token.json"
    gmail_scopes: str = "https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/gmail.send"
    gmail_redirect_uri: str = "http://localhost:8000/api/gmail/oauth/callback"

    # Safety: strict default
    auto_send_enabled: bool = False
    confidence_threshold: float = 0.70

    @property
    def AUTO_SEND_ENABLED(self) -> bool:
        return self.auto_send_enabled

    # Langfuse Observability
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"

    @property
    def langfuse_configured(self) -> bool:
        return bool(
            self.langfuse_public_key
            and self.langfuse_secret_key
            and not self.langfuse_public_key.startswith("CHANGE_ME")
        )

    # CORS
    cors_origins: str = "http://localhost:5173"

    # File paths
    upload_dir: str = "data/uploads"
    sample_knowledge_dir: str = "data/sample_knowledge_base"
    knowledge_base_dir: str = "data/knowledge_base"

settings = Settings()
