"""Application configuration management using pydantic-settings.

Loads environment variables from `.env` file with default fallbacks.
Supports dynamic switching of models, timeouts, CORS origins, and RAG paths.
"""

from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for Green Fibre AI Kiosk backend."""

    # LLM Settings
    anthropic_api_key: Optional[str] = None
    model_name: str = "claude-3-5-sonnet-20241022"

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

    # Offline / Fallback Mock Mode
    mock_llm: str = "auto"  # 'auto', 'true', or 'false'

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

    @property
    def is_mock_enabled(self) -> bool:
        """Determine whether Mock LLM mode should be active."""
        if self.mock_llm.lower() == "true":
            return True
        if self.mock_llm.lower() == "auto":
            # Auto-enable mock mode if Anthropic API key is missing or dummy
            return not self.anthropic_api_key or "your_" in self.anthropic_api_key.lower()
        return False


# Singleton settings instance
settings = Settings()
