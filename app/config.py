"""Application configuration management using pydantic-settings.

Loads environment variables from `.env` file with default fallbacks.
Supports selectable LLM providers (groq, anthropic, mock), timeouts, CORS origins, and RAG paths.
"""

import logging
from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

logger = logging.getLogger("greenie.config")


class Settings(BaseSettings):
    """Central configuration for Greenie AI Kiosk backend."""

    # Selectable LLM Provider: 'groq' | 'anthropic' | 'mock'
    llm_provider: str = "mock"

    # Groq Settings
    groq_api_key: Optional[str] = None
    groq_model_name: str = "qwen/qwen3.8-27b"

    # Anthropic Settings
    anthropic_api_key: Optional[str] = None
    model_name: str = "claude-3-5-sonnet-20241022"

    # Session & Memory
    session_idle_timeout_seconds: int = 120

    # API & CORS
    host: str = "0.0.0.0"
    port: int = 5007
    max_message_chars: int = Field(default=4000, ge=1, le=100000)
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    # RAG & Knowledge Paths
    knowledge_dir: str = "data/knowledge"
    products_file: str = "data/products.json"
    vector_store_path: str = "data/vector_store"
    embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    rag_score_threshold: float = 1.2  # Chunks with distance > 1.2 are considered weak and dropped

    # Agent Guardrails & Limits
    agent_recursion_limit: int = 10
    max_retry_wait_seconds: float = 3.0

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
    def active_model_name(self) -> str:
        """Return the active model identifier based on the selected provider."""
        provider = self.llm_provider.lower().strip()
        if provider == "groq":
            return self.groq_model_name
        if provider == "anthropic":
            return self.model_name
        return "mock-kiosk-model"


# Singleton settings instance
settings = Settings()
