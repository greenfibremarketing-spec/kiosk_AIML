"""Application configuration management using pydantic-settings.

Loads environment variables from `.env` file with default fallbacks.
Supports dynamic switching of models, timeouts, CORS origins, and explicit mock mode.
"""

import logging
from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("green_fibre.config")


class Settings(BaseSettings):
    """Central configuration for Green Fibre AI Kiosk backend."""

    # LLM Settings
    anthropic_api_key: Optional[str] = None
    model_name: str = "claude-3-5-sonnet-20241022"

    # Explicit Mock Mode: set to True to use MockKioskChatModel explicitly
    use_mock_llm: bool = False

    # Session & Memory
    session_idle_timeout_seconds: int = 120

    # API & CORS
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    # RAG & Knowledge Paths
    knowledge_dir: str = "data/knowledge"
    products_file: str = "data/products.json"
    vector_store_path: str = "data/vector_store"
    embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @property
    def cors_origin_list(self) -> List[str]:
        """Parse comma-separated CORS origins into a clean string list."""
        if not self.cors_origins:
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


# Singleton settings instance
settings = Settings()
