import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = Path(BASE_DIR) / ".env"
DB_DIR = BASE_DIR / "database"
DB_DIR.mkdir(parents=True, exist_ok=True)

# Load .env jika ada
load_dotenv()


class ServiceConfigs(BaseSettings):
    """Konfigurasi service eksternal (MCP, LLM, dsb)."""

    mcp_server_url: str = "http://localhost:5000/projectwise/mcp/"
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    ollama_host: str = "http://localhost:11434"
    llm_model: str = "gpt-4o-mini"
    embed_model: str = "text-embedding-3-small"
    llm_temperature: float = 0.0
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    log_retention: int = 90
    app_env: str = "development"

    model_config = SettingsConfigDict(env_file=str(ENV_PATH), env_file_encoding="utf-8")


class BaseConfig:
    """Base configuration class for Quart."""

    SQLALCHEMY_DATABASE_URI = (
        f"sqlite+aiosqlite:///{(DB_DIR / 'chat_memory.sqlite').as_posix()}"
    )

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-secret-key")
    SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "quart_session")

    DEBUG = False
    TESTING = False
    ENV = os.getenv("APP_ENV", "production")

    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    LOG_FORMAT = os.getenv(
        "LOG_FORMAT", "[%(asctime)s] %(levelname)s in %(module)s: %(message)s"
    )
    LOG_RETENTION = int(os.getenv("LOG_RETENTION", 90))

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


def get_config(env: str | None = None):
    if not env:
        env = os.getenv("APP_ENV", "default")
    return config_map.get(env, config_map["default"])
