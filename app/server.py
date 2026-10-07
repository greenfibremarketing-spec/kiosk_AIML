"""FastAPI HTTP backend for Greenie AI Kiosk.

Exposes REST endpoints for avatar chat (/chat), catalog products (/products),
session management (/session/reset), and health checks (/health).
Runs on host and port configured in settings (default 0.0.0.0:5007).
"""

from contextlib import asynccontextmanager
import logging
import os
import sys
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

from app.brain import ask_avatar
from app.config import settings
from app.rag import warmup_rag
from app.sessions import session_manager
from app.tools import _load_catalog

logger = logging.getLogger("greenie.server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager: warms up RAG embeddings & vector index on startup."""
    logger.info("Initializing Greenie API on port %d...", settings.port)
    try:
        warmup_timings = warmup_rag()
        logger.info(
            "RAG warmup complete (Total: %.1fms, Embeddings: %.1fms, FAISS: %.1fms)",
            warmup_timings["total_warmup_ms"],
            warmup_timings["embeddings_load_ms"],
            warmup_timings["vector_store_load_ms"],
        )
    except Exception as e:
        logger.warning("RAG warmup deferred or skipped: %s", e)
    yield
    logger.info("Shutting down Greenie API...")


api = FastAPI(
    title="Greenie AI Kiosk API",
    description="Conversational shopping assistant API for Greenie 100% upcycled rice-husk biocomposite essentials.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS setup for web kiosk frontends, React/Vue dashboards, or mobile apps
api.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str = Field(..., description="User query or spoken message.")
    session_id: Optional[str] = Field(
        default=None,
        description="Unique session identifier for multi-turn conversation memory.",
    )


class ChatResponse(BaseModel):
    reply: str
    session_id: str


class SessionResetRequest(BaseModel):
    session_id: str


@api.get("/health")
def health_check() -> Dict[str, Any]:
    """Health check returning API status and model configuration."""
    return {
        "status": "healthy",
        "service": "Greenie AI Kiosk API",
        "port": settings.port,
        "llm_provider": settings.llm_provider,
        "active_model": settings.active_model_name,
    }


@api.post("/chat", response_model=ChatResponse)
def chat_endpoint(body: ChatRequest) -> ChatResponse:
    """Chat with the Greenie AI shopping avatar."""
    sid = body.session_id or f"web-{uuid.uuid4().hex[:6]}"
    msg = body.message.strip()

    if not msg:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    try:
        reply = ask_avatar(message=msg, session_id=sid)
        return ChatResponse(reply=reply, session_id=sid)
    except Exception as e:
        logger.error("Error processing chat message: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@api.get("/products")
def get_products() -> Dict[str, Any]:
    """Return the verified catalog of Greenie products and gift bundles."""
    try:
        return _load_catalog()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load products: {e}")


@api.post("/session/reset")
def reset_session(body: SessionResetRequest) -> Dict[str, str]:
    """Reset conversational memory thread for a given session ID."""
    session_manager.reset_session(body.session_id)
    return {"status": "ok", "message": f"Memory cleared for session: {body.session_id}"}


def start():
    """Start uvicorn server directly using configured host and port."""
    uvicorn.run(
        "app.server:api",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    start()
