"""Application configuration management using pydantic-settings.

Loads environment variables from `.env` file with default fallbacks.
Supports selectable LLM providers (groq, anthropic, mock), timeouts, CORS origins, and RAG paths.
"""

import logging
from typing import List, Optional, Literal
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr

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
    llm_request_timeout_seconds: float = Field(default=20, ge=1, le=120)
    max_inflight_turns: int = Field(default=4, ge=1, le=64)
    requests_per_minute: int = Field(default=120, ge=1, le=10000)
    ws_frames_per_minute: int = Field(default=120, ge=1, le=10000)
    max_ws_connections: int = Field(default=16, ge=1, le=1000)
    max_message_chars: int = Field(default=4000, ge=1, le=100000)
    cors_origins: str = "http://localhost:3000,http://localhost:5173"

    # RAG & Knowledge Paths
    knowledge_dir: str = "data/knowledge"
    products_file: str = "data/products.json"
    product_source: Literal['json', 'greenfibre'] = 'greenfibre'
    greenfibre_api_base_url: str = 'https://api.greenfibre.org/api'
    greenfibre_api_token: Optional[SecretStr] = None
    greenfibre_api_timeout_seconds: float = Field(default=8, gt=0, le=30)
    greenfibre_catalogue_cache_seconds: float = Field(default=30, ge=0, le=300)
    greenfibre_api_max_pages: int = Field(default=20, ge=1, le=100)
    vector_store_path: str = "data/vector_store"
    embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
    rag_score_threshold: float = 1.2  # Chunks with distance > 1.2 are considered weak and dropped

    # Agent Guardrails & Limits
    agent_recursion_limit: int = 10
    max_retry_wait_seconds: float = 10.0

    # Greeny AI Operational Database Settings (greeny_ai)
    greeny_ai_mongo_url: Optional[SecretStr] = None
    greeny_ai_mongo_database: str = "greeny_ai"
    checkpointer_backend: Literal['memory', 'mongodb'] = 'memory'
    greeny_ai_retention_days: int = Field(default=90, ge=1, le=365)

    # Environment & Security
    environment: Literal['development', 'staging', 'production'] = 'development'
    kiosk_auth_secret: Optional[SecretStr] = None
    kiosk_gateway_key: Optional[SecretStr] = None
    allowed_kiosk_ids: str = "*"
    kiosk_token_ttl_seconds: int = Field(default=300, ge=30, le=86400)

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
