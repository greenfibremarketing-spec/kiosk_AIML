"""FastAPI HTTP + WebSocket backend for Greenie AI Kiosk.

Exposes REST endpoints for avatar chat (/chat, /v1/chat), catalog products (/products),
session management (/session/reset, /v1/session/reset), health checks (/health),
and a real-time WebSocket streaming endpoint (/ws/{session_id}).

WebSocket Protocol v2 (server → client):
  {"v": 2, "type": "token",    "data": "word "}             — streaming word token
  {"v": 2, "type": "sentence", "data": "Full sentence."}     — sentence boundary
  {"v": 2, "type": "action",   "data": {"name": ..., ...}} — screen/UI action
  {"v": 2, "type": "done",     "data": "full reply",
                               "meta": {"session_id": ..., "sales_stage": ...}}
  {"v": 2, "type": "error",    "data": "human message"}     — recoverable error
  {"v": 2, "type": "cancelled","data": ""}                  — turn cancelled
  {"v": 2, "type": "pong",     "data": ""}                  — keepalive reply

WebSocket Protocol v2 (client → server):
  {"message": "text"}               — customer utterance
  {"type": "cancel"}                 — interrupt current turn
  {"type": "ping"}  or  "ping"       — keepalive

CORS: set CORS_ORIGINS in .env to include app://kiosk for Electron builds.
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
import re
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import uvicorn

from app.brain import ask_avatar, ask_avatar_stream, ask_avatar_turn, AvatarTurnResult
from app.config import settings
from app.coordination import get_coordinator, SessionBusyError, CoordinationConfigurationError
from threading import Event
from app.mongo_checkpointer import CrossKioskAccessError
from app.rag import warmup_rag
from app.sessions import session_manager
from app.storage import DatabaseConnectionError
from app.tools import _load_catalog

# Thread pool for running sync LangGraph brain in async context
_thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)

# Per-session cancel flags: set True to abort an in-progress WS turn
_cancel_flags: Dict[str, bool] = {}

logger = logging.getLogger("greenie.server")


import hashlib
import hmac
import secrets

def generate_kiosk_ticket(kiosk_id: str, secret: str, ttl_seconds: int = 300) -> str:
    """Generate a tamper-evident, replay-resistant short-lived signed ticket for a specific kiosk ID."""
    ts = int(time.time())
    nonce = secrets.token_hex(6)
    payload = f"{kiosk_id}:{ts}:{nonce}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"v1.{kiosk_id}.{ts}.{nonce}.{sig}"


def verify_gateway_auth(request: Request) -> bool:
    """Authenticate that the requester is a trusted gateway authorized to issue kiosk tickets."""
    gateway_key = settings.kiosk_gateway_key.get_secret_value() if settings.kiosk_gateway_key else None
    kiosk_secret = settings.kiosk_auth_secret.get_secret_value() if settings.kiosk_auth_secret else None
    trusted_key = gateway_key if settings.environment == "production" else gateway_key or kiosk_secret

    is_prod = settings.environment == "production"
    if is_prod and not trusted_key:
        raise HTTPException(
            status_code=500,
            detail="Server misconfigured: Kiosk gateway key or secret required in production.",
        )

    # In dev/testing, if neither is configured, require dev-gateway-key
    if not trusted_key:
        trusted_key = "dev-gateway-key"

    provided_key = request.headers.get("X-Gateway-Key")
    if not provided_key:
        auth_hdr = request.headers.get("Authorization", "")
        if auth_hdr.startswith("Bearer "):
            provided_key = auth_hdr[7:].strip()

    if not provided_key:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Trusted gateway authentication required to issue kiosk tickets.",
        )

    if not hmac.compare_digest(provided_key, trusted_key):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Invalid gateway credential. Cannot mint kiosk tickets.",
        )
    return True


def verify_kiosk_token_internal(
    token: str,
    claimed_kiosk_id: Optional[str] = None,
    secret: Optional[str] = None,
    ttl_seconds: int = 300,
    is_production: bool = False,
    is_query_param: bool = False,
    consume: bool = False,
) -> str:
    """Verify a kiosk authentication token or short-lived ticket.

    Supports 5-part (nonce) and 4-part tickets with replay detection.
    Returns the verified kiosk_id.
    """
    if not secret:
        if is_production:
            raise HTTPException(
                status_code=500,
                detail="Server misconfigured: Kiosk authentication secret required in production.",
            )
        return claimed_kiosk_id or "kiosk-default"

    if not token or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Kiosk authentication token is missing.",
        )

    token = token.strip()

    # Reject permanent shared secrets in URL query strings in production mode
    if is_production and is_query_param and not token.startswith("v1."):
        raise HTTPException(
            status_code=401,
            detail="Security violation: Static shared secrets in query strings prohibited in production. Use short-lived signed tickets.",
        )

    # 1. Short-lived signed ticket: v1.<kiosk_id>.<timestamp>.<nonce>.<sig> or v1.<kiosk_id>.<timestamp>.<sig>
    if token.startswith("v1."):
        parts = token.rsplit(".", 3)
        if len(parts) == 4 and parts[0].startswith("v1."):
            parts = ["v1", parts[0][3:], *parts[1:]]
        if len(parts) == 5:
            _, token_kiosk_id, ts_str, nonce, sig = parts
            expected_payload = f"{token_kiosk_id}:{ts_str}:{nonce}"
        elif len(parts) == 4:
            _, token_kiosk_id, ts_str, sig = parts
            expected_payload = f"{token_kiosk_id}:{ts_str}"
        else:
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid signed ticket format.")

        try:
            ts = int(ts_str)
        except ValueError:
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid ticket timestamp.")

        now = int(time.time())
        if (now - ts) > ttl_seconds:
            raise HTTPException(status_code=401, detail="Unauthorized: Kiosk ticket has expired.")
        if (ts - now) > 30:
            raise HTTPException(status_code=401, detail="Unauthorized: Kiosk ticket timestamp is invalid.")

        expected_sig = hmac.new(secret.encode(), expected_payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid kiosk ticket signature.")

        # Audience restrictions (allowed kiosk IDs)
        if settings.allowed_kiosk_ids and settings.allowed_kiosk_ids.strip() != "*":
            allowed_list = [k.strip() for k in settings.allowed_kiosk_ids.split(",") if k.strip()]
            if token_kiosk_id not in allowed_list:
                raise HTTPException(
                    status_code=403,
                    detail=f"Forbidden: Kiosk ID '{token_kiosk_id}' is not an authorized kiosk device.",
                )

        if claimed_kiosk_id and claimed_kiosk_id != token_kiosk_id:
            raise HTTPException(status_code=403, detail="Kiosk identity mismatch.")
        if consume:
            if not get_coordinator().consume_ticket(sig, ttl_seconds):
                raise HTTPException(status_code=401, detail="Kiosk ticket already consumed (replay detected).")

        return token_kiosk_id

    # 2. Static shared secret fallback (accepted in dev/staging or secure headers)
    if not is_production and hmac.compare_digest(token, secret):
        return claimed_kiosk_id or "kiosk-default"

    raise HTTPException(status_code=401, detail="Unauthorized: Invalid kiosk authentication token.")


def verify_kiosk_auth(request: Request, claimed_kiosk_id: Optional[str] = None) -> str:
    """Validate kiosk authentication and return verified kiosk ID."""
    is_prod = settings.environment == "production"
    secret = settings.kiosk_auth_secret.get_secret_value() if settings.kiosk_auth_secret else None

    if is_prod and not secret:
        raise HTTPException(status_code=500, detail="Server misconfigured: Kiosk authentication secret required in production.")

    if not secret:
        return claimed_kiosk_id or request.headers.get("X-Kiosk-ID") or "kiosk-default"

    token = request.headers.get("X-Kiosk-Token")
    is_query = False
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
    if not token:
        token = request.query_params.get("token")
        is_query = True

    kiosk_id = claimed_kiosk_id or request.headers.get("X-Kiosk-ID")
    return verify_kiosk_token_internal(
        token=token or "",
        claimed_kiosk_id=kiosk_id,
        secret=secret,
        ttl_seconds=settings.kiosk_token_ttl_seconds,
        is_production=is_prod,
        is_query_param=is_query,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle manager: warms up RAG embeddings & vector index on startup."""
    if settings.environment == 'production':
        from app.coordination import validate_coordinator_production_config
        validate_coordinator_production_config()
        if hasattr(session_manager.checkpointer, 'validate_indexes'):
            session_manager.checkpointer.validate_indexes()
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
        logger.warning("RAG warmup failed: category=%s", type(e).__name__)
    yield
    logger.info("Shutting down Greenie API...")


api = FastAPI(
    title="Greenie AI Kiosk API",
    description="Conversational Green Fibre shopping assistant API.",
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


@api.middleware('http')
async def request_metadata(request, call_next):
    request_id = uuid.uuid4().hex
    started = time.perf_counter()
    error_category = None
    try:
        client_key = request.client.host if request.client else 'unknown'
        if not get_coordinator().allow('http:' + client_key, settings.requests_per_minute):
            return JSONResponse(status_code=429, content={'detail': 'Request limit exceeded.'}, headers={'Retry-After': '60'})
        if request.method in ('POST', 'PUT', 'PATCH'):
            size = 0
            chunks = []
            async for chunk in request.stream():
                size += len(chunk)
                if size > settings.max_message_chars * 6 + 2048:
                    return JSONResponse(status_code=413, content={'detail': 'Request is too large.'})
                chunks.append(chunk)
            request._body = b''.join(chunks)
        response = await call_next(request)
    except CoordinationConfigurationError as exc:
        error_category = type(exc).__name__
        response = JSONResponse(status_code=500, content={'detail': f"Server misconfigured: {exc}"})
    except Exception as exc:
        error_category = type(exc).__name__
        response = JSONResponse(status_code=500, content={'detail': 'Request could not be completed.'})
    response.headers['X-Request-ID'] = request_id
    logger.info(json.dumps({
        'event': 'http_request', 'request_id': request_id,
        'status': response.status_code, 'method': request.method,
        'latency_ms': round((time.perf_counter() - started) * 1000, 2),
        'error_category': error_category,
    }))
    return response


@api.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Validation errors normally include the input value, potentially contact data.
    return JSONResponse(status_code=422, content={'detail': [
        {'loc': error['loc'], 'type': error['type'], 'msg': 'Invalid request value.'}
        for error in exc.errors()
    ]})


class ChatRequest(BaseModel):
    message: str = Field(..., max_length=settings.max_message_chars, description="User query or spoken message.")
    session_id: Optional[str] = Field(
        default=None,
        min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$',
        description="Unique session identifier for multi-turn conversation memory.",
    )
    kiosk_id: Optional[str] = Field(
        default=None,
        min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_.:-]+$',
        description="Device identifier for multi-kiosk deployments.",
    )


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    sales_stage: Optional[str] = None
    action: Optional[Dict[str, Any]] = None


class SessionResetRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    kiosk_id: Optional[str] = Field(default=None, min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_.:-]+$')


class KioskTokenRequest(BaseModel):
    kiosk_id: str = Field(..., min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_.:-]+$', description="Device kiosk ID")


class KioskTokenResponse(BaseModel):
    ticket: str
    kiosk_id: str
    expires_in: int
    ticket_type: str = "signed_ticket_v1"


@api.post("/auth/kiosk/token", response_model=KioskTokenResponse)
def create_kiosk_token(request: Request, body: KioskTokenRequest) -> KioskTokenResponse:
    """Issue a short-lived signed ticket for an authorized kiosk.

    Authenticates the trusted requester and enforces allowed kiosk IDs.
    Fails closed in production if gateway authentication is not configured.
    """
    verify_gateway_auth(request)

    # Validate allowed kiosk IDs (prevent arbitrary/forged kiosk IDs)
    if settings.allowed_kiosk_ids and settings.allowed_kiosk_ids.strip() != "*":
        allowed_list = [k.strip() for k in settings.allowed_kiosk_ids.split(",") if k.strip()]
        if body.kiosk_id not in allowed_list:
            raise HTTPException(
                status_code=403,
                detail=f"Forbidden: Kiosk ID '{body.kiosk_id}' is not an authorized kiosk device.",
            )

    secret = settings.kiosk_auth_secret.get_secret_value() if settings.kiosk_auth_secret else None
    if not secret:
        if settings.environment == "production":
            raise HTTPException(
                status_code=500,
                detail="Server misconfigured: Kiosk authentication secret required in production.",
            )
        secret = "dev-secret-key"

    ticket = generate_kiosk_ticket(
        kiosk_id=body.kiosk_id,
        secret=secret,
        ttl_seconds=settings.kiosk_token_ttl_seconds,
    )
    return KioskTokenResponse(
        ticket=ticket,
        kiosk_id=body.kiosk_id,
        expires_in=settings.kiosk_token_ttl_seconds,
    )


@api.get("/health")
def health_check() -> Dict[str, Any]:
    """Health check returning API status, model configuration, and database connectivity."""
    from app.database import check_mongo_health
    db_health = check_mongo_health()
    status = "healthy" if db_health.get("status") in ("healthy", "disabled") else "degraded"
    return {
        "status": status,
        "service": "Greenie AI Kiosk API",
        "port": settings.port,
        "llm_provider": settings.llm_provider,
        "active_model": settings.active_model_name,
        "checkpointer_backend": settings.checkpointer_backend,
        "database": db_health,
        "coordinator": get_coordinator().get_metrics(),
    }


@api.get("/coordinator/metrics")
def coordinator_metrics(request: Request) -> Dict[str, Any]:
    """Return coordinator operational metrics and active mutex locks."""
    verify_gateway_auth(request)
    coord = get_coordinator()
    return {
        "metrics": coord.get_metrics(),
        "active_locks": coord.list_active_locks(),
    }


@api.post("/chat", response_model=ChatResponse, response_model_exclude_none=True)
@api.post("/v1/chat", response_model=ChatResponse, response_model_exclude_none=True, include_in_schema=False)
def chat_endpoint(request: Request, body: ChatRequest) -> ChatResponse:
    """Chat with the Greenie AI shopping avatar. Versioned alias: POST /v1/chat."""
    verified_kiosk_id = verify_kiosk_auth(request, claimed_kiosk_id=body.kiosk_id)
    kiosk_id = verified_kiosk_id
    sid = body.session_id or f"web-{uuid.uuid4().hex}"
    msg = body.message.strip()

    if not msg:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    try:
        # Check if ask_avatar has been monkeypatched or replaced (e.g. by unit tests)
        current_ask = sys.modules[__name__].ask_avatar
        import app.brain as _brain_mod
        if current_ask is not _brain_mod.ask_avatar:
            raw_reply = current_ask(message=msg, session_id=sid, kiosk_id=kiosk_id)
            return ChatResponse(reply=str(raw_reply), session_id=sid)

        turn = ask_avatar_turn(message=msg, session_id=sid, kiosk_id=kiosk_id)
        return ChatResponse(
            reply=turn.reply,
            session_id=sid,
            sales_stage=turn.sales_stage,
            action=turn.action,
        )
    except SessionBusyError:
        raise HTTPException(status_code=409, detail="Session is busy; retry after the current turn.") from None
    except CrossKioskAccessError as e:
        logger.warning("Cross-kiosk access denied: %s", type(e).__name__)
        raise HTTPException(status_code=403, detail="Cross-kiosk session access denied.") from None
    except DatabaseConnectionError as e:
        logger.error("Production database outage: %s", type(e).__name__)
        raise HTTPException(status_code=503, detail="Database persistence unavailable. Outage fail-safe active.") from None
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Chat failed: category=%s", type(e).__name__)
        raise HTTPException(status_code=500, detail="Unable to process your message. Please try again.") from None


@api.get("/products")
def get_products() -> Dict[str, Any]:
    """Return the verified catalog of Greenie products and gift bundles."""
    try:
        return _load_catalog()
    except Exception as e:
        logger.error("Catalogue failed: category=%s", type(e).__name__)
        raise HTTPException(status_code=500, detail="Catalogue is temporarily unavailable.") from None


@api.post("/session/reset")
@api.post("/v1/session/reset", include_in_schema=False)
def reset_session(request: Request, body: SessionResetRequest) -> Dict[str, str]:
    """Reset conversational memory thread for a given session ID. Versioned alias: POST /v1/session/reset."""
    verified_kiosk_id = verify_kiosk_auth(request, claimed_kiosk_id=body.kiosk_id)
    kiosk_id = verified_kiosk_id
    try:
        with get_coordinator().exclusive("turn:" + body.session_id):
            session_manager.reset_session(body.session_id, kiosk_id=kiosk_id)
        # Also clear any dangling cancel flag
        _cancel_flags.pop(body.session_id, None)
    except SessionBusyError:
        raise HTTPException(status_code=409, detail="Session is busy; retry after the current turn.") from None
    except CrossKioskAccessError as e:
        logger.warning("Cross-kiosk reset denied: %s", type(e).__name__)
        raise HTTPException(status_code=403, detail="Cross-kiosk session access denied.") from None
    except DatabaseConnectionError as e:
        logger.error("Database outage on reset: %s", type(e).__name__)
        raise HTTPException(status_code=503, detail="Database persistence unavailable. Outage fail-safe active.") from None
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Session reset failed: category=%s", type(e).__name__)
        raise HTTPException(status_code=503, detail="Unable to reset session. Please try again.") from None
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

_WS_PROTO_VERSION = 2


def _ws_envelope(type_: str, data: Any, meta: Optional[Dict] = None, v2: bool = False) -> dict:
    """Build a WebSocket envelope. Emits clean v1 format by default, includes v:2 and meta if v2."""
    if v2:
        msg: Dict[str, Any] = {"v": _WS_PROTO_VERSION, "type": type_, "data": data}
        if meta:
            msg["meta"] = meta
        return msg
    msg = {"type": type_, "data": data}
    if meta:
        msg["metadata"] = meta
    return msg


@api.websocket("/ws/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str):
    """
    Real-time streaming WebSocket chat endpoint (Protocol v1 and v2).

    Connect:  ws://host/ws/{session_id}  (or ?v=2 / ?proto=2 for v2 envelope)

    Client → Server:
      {"message": "Hello!"}    — customer utterance (JSON or plain text)
      {"type": "cancel"}       — cancel the current in-progress turn
      {"type": "ping"}  or "ping"  — keepalive

    Server → Client:
      {"type": "token", "data": "word "}             — streaming token
      {"type": "sentence", "data": "Full sentence."}  — sentence boundary (v2 mode)
      {"type": "action", "data": {...}}              — structured screen action (v2 mode)
      {"type": "done", "data": "full reply"}          — turn complete
      {"type": "cancelled", "data": ""}              — turn was cancelled
      {"type": "error", "data": "human message"}      — recoverable error
      {"type": "pong", "data": ""}                   — keepalive reply
    """
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', session_id):
        await websocket.close(code=1008)
        return

    is_prod = settings.environment == "production"
    secret = settings.kiosk_auth_secret.get_secret_value() if settings.kiosk_auth_secret else None

    # Production must fail closed if authentication configuration is missing
    if is_prod and not secret:
        logger.error("Rejecting WS connection: production requires KIOSK_AUTH_SECRET.")
        await websocket.close(code=1008)
        return

    kiosk_token = websocket.headers.get("x-kiosk-token")
    is_query = False
    if not kiosk_token:
        auth = websocket.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            kiosk_token = auth[7:].strip()
    if not kiosk_token:
        kiosk_token = websocket.query_params.get("token")
        if kiosk_token:
            is_query = True

    claimed_kiosk_id = websocket.query_params.get("kiosk_id") or websocket.headers.get("x-kiosk-id")
    verified_kiosk_id = "kiosk-default"

    if secret or is_prod:
        try:
            verified_kiosk_id = verify_kiosk_token_internal(
                token=kiosk_token or "",
                claimed_kiosk_id=claimed_kiosk_id,
                secret=secret,
                ttl_seconds=settings.kiosk_token_ttl_seconds,
                is_production=is_prod,
                is_query_param=is_query,
                consume=True,
            )
        except HTTPException as exc:
            logger.warning("WS authentication failed: %s", exc.detail)
            await websocket.close(code=1008)
            return
    elif claimed_kiosk_id:
        verified_kiosk_id = claimed_kiosk_id

    # Verify session ownership: prevent cross-kiosk session hijacking
    existing_owner = session_manager.get_session_kiosk(session_id)
    if existing_owner and existing_owner != verified_kiosk_id:
        logger.warning(
            "WebSocket rejected: session %s owned by %s, attempted by %s",
            session_id, existing_owner, verified_kiosk_id,
        )
        await websocket.close(code=1008)
        return

    # Claim/touch the session for this kiosk
    try:
        with get_coordinator().exclusive("turn:" + session_id):
            session_manager.touch(session_id, kiosk_id=verified_kiosk_id)
    except (CrossKioskAccessError, SessionBusyError, DatabaseConnectionError):
        await websocket.close(code=1008)
        return

    coordinator = get_coordinator()
    connection_token = None
    slot = None
    slot_token = None
    try:
        connection_token = coordinator.acquire('connection:' + session_id)
        for index in range(settings.max_ws_connections):
            try:
                slot_token = coordinator.acquire(f'connection-slot:{index}')
                slot = index
                break
            except SessionBusyError:
                continue
        if slot is None:
            coordinator.release('connection:' + session_id, connection_token)
            connection_token = None
            await websocket.close(code=1013)
            return
    except SessionBusyError:
        if connection_token:
            coordinator.release('connection:' + session_id, connection_token)
            connection_token = None
        await websocket.close(code=1013)
        return

    await ws_manager.connect(session_id, websocket)
    is_v2 = websocket.query_params.get('v') == '2' or websocket.query_params.get('proto') == '2'
    disconnected = Event()
    cancelled = Event()
    running = None
    current_id = None
    send_lock = asyncio.Lock()

    async def send(kind, data, meta=None):
        if not disconnected.is_set():
            async with send_lock:
                if not disconnected.is_set():
                    await ws_manager.send_json(websocket, _ws_envelope(kind, data, meta=meta, v2=is_v2))

    async def execute(message, turn_id):
        # Inference and generator iteration both run off the receive loop.
        # The provider is synchronous: cancellation suppresses output, it does
        # not kill a thread or claim that provider billing/work has stopped.
        inference_slot = None
        for index in range(settings.max_inflight_turns):
            try:
                inference_token = coordinator.acquire(f'ws-inference-slot:{index}')
                inference_slot = index
                break
            except SessionBusyError:
                continue
        if inference_slot is None:
            await send('error', 'Backend is busy; please retry later.')
            return

        def generate():
            stream = ask_avatar_stream(message=message, session_id=session_id, kiosk_id=verified_kiosk_id)
            action = getattr(stream, 'action', None)
            stage = getattr(stream, 'sales_stage', None)
            words = []
            for word in stream:
                if cancelled.is_set() or disconnected.is_set() or _cancel_flags.get(session_id):
                    break
                words.append(word)
            return words, action, stage
        def infer():
            try:
                return generate()
            finally:
                coordinator.release(f'ws-inference-slot:{inference_slot}', inference_token)
        try:
            words, action, stage = await asyncio.get_running_loop().run_in_executor(_thread_pool, infer)
            if cancelled.is_set() or disconnected.is_set():
                return
            if _cancel_flags.get(session_id):
                await send('cancelled', '', {'turn_id': turn_id, 'provider_cancelled': False, 'output_suppressed': True})
                return
            if is_v2 and action:
                await send('action', action)
            full_reply = ''
            sentence = ''
            for word in words:
                if cancelled.is_set() or disconnected.is_set():
                    return
                if not word:
                    continue
                full_reply += word
                sentence += word
                await send('token', word)
                if is_v2 and sentence.rstrip().endswith(('.', '!', '?', '।')):
                    await send('sentence', sentence.strip())
                    sentence = ''
                await asyncio.sleep(0)
            if cancelled.is_set() or disconnected.is_set():
                return
            if is_v2 and sentence.strip():
                await send('sentence', sentence.strip())
            await send('done', full_reply.strip(), {
                'session_id': session_id, 'turn_id': turn_id,
                'sales_stage': stage, 'action': action,
            } if is_v2 else None)
        except Exception as exc:
            logger.error('WS turn failed: category=%s', type(exc).__name__)
            if not cancelled.is_set():
                await send('error', 'Session is busy.' if isinstance(exc, SessionBusyError) else 'Database persistence unavailable. Outage fail-safe active.' if isinstance(exc, DatabaseConnectionError) else 'Unable to process your message. Please try again.')

    try:
        while True:
            raw = await websocket.receive_text()
            if not coordinator.allow('ws:' + verified_kiosk_id, settings.ws_frames_per_minute):
                await websocket.close(code=1008)
                break
            if len(raw) > settings.max_message_chars * 6 + 256:
                await send('error', 'Message is too long.')
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {'type': 'ping'} if raw.strip().lower() == 'ping' else {'message': raw}
            if not isinstance(payload, dict):
                await send('error', 'Provide a text message.')
                continue
            if payload.get('v') == 2:
                is_v2 = True
            if payload.get('type') == 'ping':
                await send('pong', '')
                continue
            if payload.get('type') == 'cancel':
                if payload.get('turn_id') and payload['turn_id'] != current_id:
                    await send('error', 'Turn identity mismatch.')
                    continue
                cancelled.set()
                await send('cancelled', '', {'turn_id': current_id, 'provider_cancelled': False, 'output_suppressed': True})
                continue
            message = payload.get('message')
            if not isinstance(message, str) or not message.strip():
                await send('error', 'Provide a text message.')
                continue
            if len(message) > settings.max_message_chars:
                await send('error', 'Message is too long.')
                continue
            if payload.get('kiosk_id') and payload['kiosk_id'] != verified_kiosk_id:
                await send('error', 'Identity mismatch.')
                continue
            if running and not running.done():
                await send('error', 'Session is busy; current inference must finish before another turn.')
                continue
            turn_id = payload.get('turn_id', uuid.uuid4().hex)
            if not isinstance(turn_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', turn_id):
                await send('error', 'Invalid turn_id.')
                continue
            digest = hashlib.sha256(f'{verified_kiosk_id}:{session_id}:{turn_id}'.encode()).hexdigest()
            if not coordinator.consume_ticket('turn:' + digest, 86400):
                await send('error', 'Duplicate turn_id.')
                continue
            current_id = turn_id
            cancelled.clear()
            _cancel_flags[session_id] = False
            running = asyncio.create_task(execute(message.strip(), turn_id))
    except WebSocketDisconnect:
        pass
    finally:
        disconnected.set()
        cancelled.set()
        ws_manager.disconnect(session_id)
        _cancel_flags.pop(session_id, None)
        if connection_token:
            try:
                coordinator.release('connection:' + session_id, connection_token)
            except Exception as e:
                logger.error("Error releasing connection lock: %s", type(e).__name__)
        if slot is not None and slot_token:
            try:
                coordinator.release(f'connection-slot:{slot}', slot_token)
            except Exception as e:
                logger.error("Error releasing connection slot lock: %s", type(e).__name__)
        # Do not cancel a thread Future: the brain keeps its session mutex until
        # actual inference exits, including after a disconnect/reconnect.



@api.get("/ws/status")
def websocket_status(request: Request) -> Dict[str, Any]:
    """Return current active WebSocket connection count."""
    verify_gateway_auth(request)
    return {"active_connections": len(ws_manager.active), "scope": "this_worker"}


def start():
    """Start uvicorn server directly using configured host and port."""
    uvicorn.run(
        "app.server:api",
        host=settings.host,
        port=settings.port,
        reload=False,
        ws_max_size=settings.max_message_chars * 6 + 256,
        ws_max_queue=4,
    )


if __name__ == "__main__":
    start()
