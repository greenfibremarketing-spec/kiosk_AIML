"""FastAPI HTTP + WebSocket backend for Greenie AI Kiosk.

Exposes REST endpoints for avatar chat (/chat), catalog products (/products),
session management (/session/reset), health checks (/health), and a real-time
WebSocket streaming endpoint (/ws/{session_id}).
Runs on host and port configured in settings (default 0.0.0.0:5007).
"""

from contextlib import asynccontextmanager
import asyncio
import concurrent.futures
import json
import logging
import os
import sys
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

from app.brain import ask_avatar, ask_avatar_stream
from app.config import settings
from app.rag import warmup_rag
from app.sessions import session_manager
from app.tools import _load_catalog

# Thread pool for running sync LangGraph brain in async context
_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)

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


# ── WebSocket Connection Manager ────────────────────────────────────────────

class ConnectionManager:
    """Tracks all active WebSocket connections."""

    def __init__(self) -> None:
        self.active: Dict[str, WebSocket] = {}

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active[session_id] = websocket
        logger.info("WS connected: session=%s  total=%d", session_id, len(self.active))

    def disconnect(self, session_id: str) -> None:
        self.active.pop(session_id, None)
        logger.info("WS disconnected: session=%s  total=%d", session_id, len(self.active))

    async def send_json(self, websocket: WebSocket, data: dict) -> None:
        await websocket.send_text(json.dumps(data))


ws_manager = ConnectionManager()


# ── WebSocket Endpoint ───────────────────────────────────────────────────────

@api.websocket("/ws/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str):
    """
    Real-time streaming WebSocket chat endpoint.

    Connect:  ws://host/ws/{session_id}
    Send:     JSON  {"message": "Hello!"}
              or plain text string "Hello!"
    Receive:  {"type": "token",    "data": "word "}      — streaming token
              {"type": "done",     "data": "full reply"}  — turn complete
              {"type": "error",    "data": "msg"}         — error occurred
              {"type": "pong",     "data": ""}            — keepalive reply
    """
    await ws_manager.connect(session_id, websocket)
    loop = asyncio.get_event_loop()

    try:
        while True:
            # Receive a message (JSON or plain string)
            raw = await websocket.receive_text()

            # Keepalive ping
            if raw.strip().lower() in ("ping", '{"type":"ping"}'):
                await ws_manager.send_json(websocket, {"type": "pong", "data": ""})
                continue

            # Parse message
            try:
                payload = json.loads(raw)
                message = payload.get("message", "").strip()
            except (json.JSONDecodeError, AttributeError):
                message = raw.strip()

            if not message:
                await ws_manager.send_json(websocket, {"type": "error", "data": "Message cannot be empty."})
                continue

            logger.info("WS message: session=%s  msg=%.60s", session_id, message)

            # Stream tokens back using ask_avatar_stream (runs in thread pool)
            full_reply = ""
            try:
                # ask_avatar_stream is a sync generator — wrap in thread executor
                def _stream_tokens():
                    return list(ask_avatar_stream(message=message, session_id=session_id))

                tokens = await loop.run_in_executor(_thread_pool, _stream_tokens)

                for token in tokens:
                    if token:
                        full_reply += token
                        await ws_manager.send_json(websocket, {"type": "token", "data": token})

            except Exception as e:
                logger.error("WS streaming error: session=%s  err=%s", session_id, e, exc_info=True)
                # Fallback to non-streaming ask_avatar
                try:
                    full_reply = await loop.run_in_executor(
                        _thread_pool,
                        lambda: ask_avatar(message=message, session_id=session_id)
                    )
                    # Send the whole reply as tokens word by word
                    for word in full_reply.split(" "):
                        token = word + " "
                        await ws_manager.send_json(websocket, {"type": "token", "data": token})
                except Exception as e2:
                    await ws_manager.send_json(websocket, {"type": "error", "data": str(e2)})
                    continue

            # Signal turn complete with full assembled reply
            await ws_manager.send_json(websocket, {"type": "done", "data": full_reply.strip()})

    except WebSocketDisconnect:
        ws_manager.disconnect(session_id)
    except Exception as e:
        logger.error("WS unexpected error: session=%s  err=%s", session_id, e, exc_info=True)
        ws_manager.disconnect(session_id)


@api.get("/ws/status")
def websocket_status() -> Dict[str, Any]:
    """Return current active WebSocket connection count."""
    return {
        "active_connections": len(ws_manager.active),
        "session_ids": list(ws_manager.active.keys()),
    }


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
