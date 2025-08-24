# projectwise/config.py
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = Path(BASE_DIR) / ".env"
DB_DIR = BASE_DIR / "database"
DB_DIR.mkdir(parents=True, exist_ok=True)

# Load .env
load_dotenv(ENV_PATH)


class ServiceConfigs(BaseSettings):
    """Konfigurasi service eksternal (MCP, LLM, dsb)."""

    # ====================================
    # Model dan parameter LLM
    # ====================================
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://api.openai.com")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "gpt-4o-mini")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    max_token: int = 16000  # context window 32k token untuk ringkasan - qwen25-72b-instruct
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", 0.2))
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    embedding_model_api_key: str = os.getenv("EMBEDDING_MODEL_API_KEY", "")

    # ====================================
    # MCP
    # ====================================
    mcp_server_url: str = os.getenv(
        "MCP_SERVER_URL", "http://localhost:5000/projectwise/mcp/"
    )

    # ====================================
    # Agent Workflow
    # ====================================
    intent_classification_threshold: float = float(
        os.getenv("INTENT_CLASSIFICATION_THRESHOLD", 0.60)
    )

    # ====================================
    # Database Vector Mem0ai
    # ====================================
    qdrant_llm_provider: str = "openai"
    qdrant_host: str = os.getenv("QDRANT_HOST", "localhost")
    qdrant_port: int = int(os.getenv("QDRANT_PORT", 6333))
    vector_dim: int = int(os.getenv("VECTOR_DIM", "1536"))
    collection_name: str = "projectwise_client"

    # ====================================
    # Base config ENV
    # ====================================
    max_concurrent_proccess: int = int(os.getenv("MAX_CONCURRENT_PROCCESS", "8"))
    max_cpu_workers: int = max(1, int(os.getenv("MAX_CPU_WORKERS", "4")))

    log_retention: int = int(os.getenv("LOG_RETENTION", 90))
    app_env: str = os.getenv("APP_ENV", "development")
    model_config = SettingsConfigDict(
        env_file=str(ENV_PATH), env_file_encoding="utf-8", extra="ignore"
    )


class BaseConfig:
    """Base configuration class for Quart."""

    # ====================
    # Database Config
    # ====================
    SQLALCHEMY_DATABASE_URI = (
        f"sqlite+aiosqlite:///{(DB_DIR / 'chat_memory.sqlite').as_posix()}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ====================
    # App Config
    # ====================
    ENV = os.getenv("APP_ENV", "development")
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-secret-key")
    SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "quart_session")
    DEBUG = False
    TESTING = False

    # ====================
    # Logger Config
    # ====================
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_FORMAT = os.getenv(
        "LOG_FORMAT", "[%(asctime)s] %(levelname)s in %(module)s: %(message)s"
    )
    LOG_RETENTION = int(os.getenv("LOG_RETENTION", 90))
    LOG_MODE = os.getenv("LOG_MODE", "file")  # file | stdout | socket
    LOG_CONSOLE = os.getenv("LOG_CONSOLE", "true").lower() == "true"
    LOG_CONSOLE_LEVEL = os.getenv("LOG_CONSOLE_LEVEL", "") or None
    LOG_MONTH_FORMAT = os.getenv("LOG_MONTH_FORMAT", "%Y-%m")
    LOG_USE_UTC = os.getenv("LOG_USE_UTC", "false").lower() == "true"

    LOG_SOCKET_HOST = os.getenv("LOG_SOCKET_HOST", "127.0.0.1")
    LOG_SOCKET_PORT = int(os.getenv("LOG_SOCKET_PORT", "9020"))
    _LOG_ROOT_DIR_RAW = os.getenv("LOG_ROOT_DIR", "").strip()
    LOG_ROOT_DIR = Path(_LOG_ROOT_DIR_RAW).resolve() if _LOG_ROOT_DIR_RAW else None

    LOG_SOCKET_HOST = os.getenv("LOG_SOCKET_HOST", "127.0.0.1")
    LOG_SOCKET_PORT = int(os.getenv("LOG_SOCKET_PORT", "9020"))

    JSON_SORT_KEYS = False
    JSONIFY_PRETTYPRINT_REGULAR = False


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    ENV = "development"


class TestingConfig(BaseConfig):
    TESTING = True
    DEBUG = True
    ENV = "testing"


class ProductionConfig(BaseConfig):
    DEBUG = False
    ENV = "production"


config_map = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}


def get_config(env: str | None = None) -> type[BaseConfig]:
    """Return the configuration class corresponding to the given environment."""
    if not env:
        env = os.getenv("APP_ENV", "default")
    return config_map.get(env, config_map["default"])
