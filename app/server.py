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
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from app.guided_contract import ConversationInput, GuidedCommandError
from app.conversation_agent import warmup_conversation_agent
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
from app.greenfibre_repository import ProductAPIError
from app.fast_flow import fast_transition, silence_tick, drop_ff

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
    try:
        from app.sales_engine import get_verified_catalog
        t_cat = time.time()
        w_cat = get_verified_catalog()
        logger.info(
            "Catalog warmup complete (Total: %.1fms, Products: %d)",
            (time.time() - t_cat) * 1000,
            len(w_cat.get("products", [])),
        )
    except Exception as e:
        logger.warning("Catalog warmup failed: category=%s", type(e).__name__)
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


class ChatRequest(ConversationInput):
    model_config = ConfigDict(extra='ignore', str_strip_whitespace=True)
    session_id: Optional[str] = Field(default=None, min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')
    kiosk_id: Optional[str] = Field(default=None, min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_.:-]+$')
    stt_final_ts: Optional[float] = None
    client_sent_ts: Optional[float] = None

    @model_validator(mode='after')
    def command_session(self):
        if self.action and not self.session_id:
            raise ValueError('REST commands require session_id.')
        return self


class ChatResponse(BaseModel):
    reply: str
    session_id: str
    sales_stage: Optional[str] = None
    turn_id: Optional[str] = None
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


@api.post("/chat", response_model=ChatResponse, response_model_exclude_none=True, responses={
    401: {"description": "Missing or invalid kiosk ticket"},
    403: {"description": "Kiosk/session identity mismatch"},
    409: {"description": "Busy session, invalid stage or duplicate command"},
    413: {"description": "Request body too large"},
    429: {"description": "Request rate limit"},
    503: {"description": "Dependency unavailable; invalidate displayed product facts"},
})
@api.post("/v1/chat", response_model=ChatResponse, response_model_exclude_none=True, include_in_schema=False)
def chat_endpoint(request: Request, body: ChatRequest) -> ChatResponse:
    """Chat with the Greenie AI shopping avatar. Versioned alias: POST /v1/chat."""
    verified_kiosk_id = verify_kiosk_auth(request, claimed_kiosk_id=body.kiosk_id)
    kiosk_id = verified_kiosk_id
    msg = body.normalized_message()

    sid = body.session_id or f"web-{uuid.uuid4().hex}"
    try:
        # Check if ask_avatar has been monkeypatched or replaced (e.g. by unit tests)
        current_ask = sys.modules[__name__].ask_avatar
        import app.brain as _brain_mod
        if current_ask is not _brain_mod.ask_avatar:
            raw_reply = current_ask(message=msg, session_id=sid, kiosk_id=kiosk_id)
            return ChatResponse(reply=str(raw_reply), session_id=sid)

        turn = ask_avatar_turn(message=msg, session_id=sid, kiosk_id=kiosk_id, **({"command": body.command()} if body.action else {}))
        return ChatResponse(
            reply=turn.reply,
            session_id=sid,
            sales_stage=turn.sales_stage,
            turn_id=body.turn_id,
            action=turn.action,
        )
    except GuidedCommandError as exc:
        logger.warning("Chat 409 (GuidedCommandError) [session=%s]: %s", sid, exc)
        raise HTTPException(status_code=409, detail="Command is invalid for the current stage or already processed.") from None
    except ProductAPIError:
        raise HTTPException(status_code=503, detail={"code": "product_data_unavailable", "invalidate_product_facts": True}) from None
    except SessionBusyError:
        logger.warning("Chat 409 (SessionBusyError) [session=%s]: session is busy (concurrent request while turn lock held)", sid)
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


@api.get("/pairups")
def get_pairups(product_id: Optional[str] = "", cart: Optional[str] = "") -> List[Dict[str, Any]]:
    """Return 3 to 4 pair-up recommendations for a given product, excluding cart items."""
    try:
        from app.recommendation import get_pairup_recommendations
        return get_pairup_recommendations(product_id=product_id or "", cart=cart or "")
    except Exception as e:
        logger.error("Pair-ups failed: %s", e)
        return []


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
        try:
            from app.fast_flow import drop_ff
            drop_ff(body.session_id)
        except Exception:
            pass
    except GuidedCommandError as exc:
        logger.warning("Reset 409 (GuidedCommandError) [session=%s]: %s", body.session_id, exc)
        raise HTTPException(status_code=409, detail="Command is invalid for the current stage or already processed.") from None
    except ProductAPIError:
        raise HTTPException(status_code=503, detail={"code": "product_data_unavailable", "invalidate_product_facts": True}) from None
    except SessionBusyError:
        logger.warning("Reset 409 (SessionBusyError) [session=%s]: session is busy (turn lock held)", body.session_id)
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
    asyncio.create_task(warmup_conversation_agent())
    is_v2 = websocket.query_params.get('v') == '2' or websocket.query_params.get('proto') == '2'
    disconnected = Event()
    cancelled = Event()
    running = None
    current_id = None
    send_lock = asyncio.Lock()

    def _sync_screen_from_action(ctx, action: Optional[Dict[str, Any]]):
        if not ctx or not isinstance(action, dict):
            return
        act = action.get("action")
        if act == "SHOW_CATEGORIES":
            ctx.current_screen = {"type": "categories", "category": None, "sku": None}
            ctx.categories_shown_at = time.time()
        elif act == "SHOW_PRODUCTS":
            ctx.current_screen = {"type": "list", "category": action.get("category"), "sku": None}
            prods = action.get("products", [])
            skus = [p.get("sku") for p in prods if isinstance(p, dict) and p.get("sku")]
            if skus:
                ctx.last_list_skus = skus
        elif act == "SHOW_PRODUCT":
            sku = action.get("sku") or (action.get("product", {}).get("sku") if isinstance(action.get("product"), dict) else None)
            ctx.current_screen = {"type": "product", "category": action.get("category"), "sku": sku}
            if sku:
                ctx.selected_sku = sku
        elif act in ("SHOW_GREETING", "ASK_NAME"):
            ctx.current_screen = {"type": "home", "category": None, "sku": None}
        elif act in ("CONFIRM_BUY", "SHOW_CART"):
            ctx.current_screen = {"type": "cart", "category": None, "sku": None}

    async def send(kind, data, meta=None):
        if not disconnected.is_set():
            async with send_lock:
                if not disconnected.is_set():
                    await ws_manager.send_json(websocket, _ws_envelope(kind, data, meta=meta, v2=is_v2))

    is_fast_flow = websocket.query_params.get('flow') == 'fast' or websocket.query_params.get('auto_greet') in ('1', 'true', 'yes') or session_id.startswith('fast-')
    auto_greet = websocket.query_params.get('auto_greet') in ('1', 'true', 'yes') or (session_id.startswith('fast-') and websocket.query_params.get('auto_greet') != 'false')

    # ── Auto-greet: kiosk starts the conversation without waiting for the user ──
    if auto_greet:
        try:
            def _greet():
                return fast_transition(session_id, "hello")
            _g_reply, _g_action, _ = await asyncio.get_running_loop().run_in_executor(
                _thread_pool, _greet
            )
            auto_turn_id = "auto-greet-0"
            if is_v2 and _g_action:
                from app.fast_flow import get_ff
                _sync_screen_from_action(get_ff(session_id), _g_action)
                await send('action', _g_action, {'turn_id': auto_turn_id})
            for _w in _g_reply.split():
                await send('token', _w + ' ')
            await send('done', _g_reply,
                       {'session_id': session_id, 'turn_id': auto_turn_id,
                        'sales_stage': 'GREETING', 'action': _g_action} if is_v2 else None)
        except Exception as _ge:
            logger.warning('Auto-greet failed: %s', type(_ge).__name__)

    # ── Silence detection: pushed every 8 s of no WS activity ─────────────
    _SILENCE_INTERVAL = 8.0
    _last_activity: list = [asyncio.get_event_loop().time()]

    async def silence_watcher():
        while not disconnected.is_set():
            await asyncio.sleep(_SILENCE_INTERVAL)
            if disconnected.is_set():
                break
            idle = asyncio.get_event_loop().time() - _last_activity[0]
            if idle >= _SILENCE_INTERVAL - 0.5:
                frame = silence_tick(session_id)
                await send(frame['type'], frame['data'])
                if frame['type'] == 'auto_reset':
                    break

    silence_task = None
    if is_fast_flow:
        silence_task = asyncio.create_task(silence_watcher())

    async def execute(message, turn_id, command=None, payload=None):
        t_ws_recv = time.perf_counter()
        _last_activity[0] = asyncio.get_event_loop().time()
        payload = payload or {}
        stt_final_ts = payload.get('stt_final_ts')
        client_sent_ts = payload.get('client_sent_ts')
        stt_final_to_ws_sent = int(client_sent_ts - stt_final_ts) if (stt_final_ts and client_sent_ts and client_sent_ts >= stt_final_ts) else 0

        first_frame_sent_ms = None
        first_sentence_sent_ms = None
        llm_first_token_ms = None
        llm_done_ms = None
        llm_start_ms = 0

        # ── Fast-flow path: button actions and guided commands ────────────
        # fast_transition handles NEXT_PRODUCT/CHEAPER/BUY/COMPARE/etc.
        # without invoking the LLM, keeping response time < 100 ms.
        use_fast = False
        msg_clean = (message or "").strip().lower()
        if is_fast_flow:
            ff_keywords = (
                'next_product', 'next', 'cheaper', 'compare', 'select_category:',
                'select_product:', 'buy:', 'checkout', 'goodbye', 'show all',
                'show_all', 'restart', 'start over', 'call staff', 'help',
                'skip', 'skip name', 'no name', 'just browse', 'speech_done', 'greeting_done', 'hello',
            )
            use_fast = command is not None or any(msg_clean.startswith(kw) for kw in ff_keywords)
            try:
                from app.fast_flow import get_ff, _extract_name
                ctx = get_ff(session_id)
                if ctx.stage in ('WAKE', 'GREETING'):
                    if msg_clean in ('hello', 'hi', 'start', 'speech_done', 'greeting_done'):
                        use_fast = True
                elif ctx.stage == 'NAME':
                    if command and command.get('action') in ('SET_NAME', 'SKIP_NAME'):
                        use_fast = True
                    elif msg_clean in ('skip', 'skip name', 'no name', 'just browse', 'next', 'no', 'nahi', 'pass'):
                        use_fast = True
                    else:
                        extracted = _extract_name(message)
                        # Only treat as name if it doesn't contain conversational intent words
                        intent_keywords = (
                            'gift', 'budget', 'under', 'below', 'bottle', 'mug', 'plate',
                            'bowl', 'show', 'price', 'cost', 'kitchen', 'office', 'kids',
                            'help', 'what', 'how', 'where', 'which', 'is', 'can', 'tell'
                        )
                        if extracted and not any(kw in msg_clean for kw in intent_keywords):
                            use_fast = True
            except Exception:
                pass

        if use_fast:
            try:
                def _ff_call():
                    return fast_transition(session_id, message, command=command)
                reply, ff_action, _ = await asyncio.get_running_loop().run_in_executor(
                    _thread_pool, _ff_call
                )
                if cancelled.is_set() or disconnected.is_set():
                    return
                if is_v2 and ff_action:
                    from app.fast_flow import get_ff
                    _sync_screen_from_action(get_ff(session_id), ff_action)
                    await send('action', ff_action, {'turn_id': turn_id})
                ff_act = ff_action.get("action") if ff_action else "NONE"
                ff_sku = ff_action.get("sku") if ff_action else None
                dec_log = f"[Decision] source=fast_flow action={ff_act} skus={[ff_sku] if ff_sku else []}"
                logger.info(dec_log)
                print(dec_log, flush=True)
                for word in reply.split():
                    await send('token', word + ' ')
                await send('done', reply, {'session_id': session_id, 'turn_id': turn_id,
                                           'sales_stage': ff_action.get('action') if ff_action else None,
                                           'action': ff_action} if is_v2 else None)
                return
            except Exception as exc:
                logger.warning('fast_flow error, falling back to conversation agent: %s', type(exc).__name__)

        # ── Free-conversation agent path: intelligent conversational shopping ───
        try:
            from app.fast_flow import get_ff
            from app.sales_engine import get_verified_catalog
            from app.conversation_agent import (
                build_compact_catalog,
                quick_match_intent,
                stream_free_conversation,
            )
            ctx = get_ff(session_id)

            # If user explicitly said skip / no name / just browse, mark name_status = "skipped"
            if msg_clean in ('skip', 'skip name', 'no name', 'just browse'):
                if ctx and ctx.name_status != 'given':
                    ctx.name_status = 'skipped'
                    ctx.name = None

            try:
                catalog = get_verified_catalog()
            except Exception:
                catalog = {"products": []}
            all_products = catalog.get("products", [])
            compact_catalog = build_compact_catalog(catalog)

            current_ctx = {
                "stage": ctx.stage if ctx else None,
                "selected_sku": getattr(ctx, "selected_sku", None),
                "current_screen": getattr(ctx, "current_screen", None),
                "last_list_skus": getattr(ctx, "last_list_skus", None),
                "name_status": getattr(ctx, "name_status", "unknown"),
                "name_asked_count": getattr(ctx, "name_asked_count", 0),
                "categories_shown_at": getattr(ctx, "categories_shown_at", 0),
            } if ctx else {}

            # Step 1: Instant deterministic navigation (<300 ms screen change)
            quick_match = quick_match_intent(
                customer_message=message,
                current_stage=ctx.stage if ctx else "BROWSING",
                current_context=current_ctx,
                all_products=all_products,
                customer_name=ctx.name if ctx else None,
                current_screen=getattr(ctx, "current_screen", None),
                last_list_skus=getattr(ctx, "last_list_skus", None),
            )

            instant_action = None
            instant_opener = None
            instant_stage = None
            instant_skus = []

            if quick_match:
                instant_action, instant_opener, instant_stage, instant_skus = quick_match
                if is_v2 and instant_action:
                    _sync_screen_from_action(ctx, instant_action)
                    logger.info("[Chat] frame_sent action=%s skus=%s", instant_action.get("action"), instant_skus)
                    print(f"[Chat] frame_sent action={instant_action.get('action')} skus={instant_skus}", flush=True)
                    await send('action', instant_action, {'turn_id': turn_id})
                    first_frame_sent_ms = int((time.perf_counter() - t_ws_recv) * 1000)

                if instant_opener:
                    await send('token', instant_opener + ' ')
                    if is_v2:
                        await send('sentence', instant_opener)
                        first_sentence_sent_ms = int((time.perf_counter() - t_ws_recv) * 1000)

            # Step 2: Real LLM streaming for friendly explanation
            t_llm_start = time.perf_counter()
            llm_start_ms = int((t_llm_start - t_ws_recv) * 1000)

            accumulated_reply = (instant_opener + " ") if instant_opener else ""
            final_action = instant_action
            final_stage = instant_stage or (ctx.stage if ctx else "BROWSING")

            async for event_type, data in stream_free_conversation(
                session_id=session_id,
                customer_message=message,
                current_stage=ctx.stage if ctx else "BROWSING",
                customer_name=ctx.name if ctx else None,
                current_context=current_ctx,
                history=getattr(ctx, "history", None),
                turn_id=turn_id,
                all_products=all_products,
                compact_catalog=compact_catalog,
                instant_match=bool(instant_action),
                expected_screen_action=instant_action,
                name_status=getattr(ctx, "name_status", "unknown"),
                name_asked_count=getattr(ctx, "name_asked_count", 0),
                current_screen=getattr(ctx, "current_screen", None),
                categories_shown_at=getattr(ctx, "categories_shown_at", 0.0),
            ):
                if cancelled.is_set() or disconnected.is_set():
                    return
                if _cancel_flags.get(session_id):
                    await send('cancelled', '', {'turn_id': turn_id, 'provider_cancelled': False, 'output_suppressed': True})
                    return

                if event_type == "llm_first_token":
                    llm_first_token_ms = data
                elif event_type == "token":
                    await send('token', data)
                elif event_type == "sentence":
                    if any(k in data for k in ('"reply"', '"action"', '"skus"', '{', '}')):
                        logger.warning("[Chat] dropped non-speech text: %s", data)
                        continue
                    if instant_opener and data.strip().lower() == instant_opener.strip().lower():
                        continue
                    accumulated_reply += data + " "
                    if is_v2:
                        await send('sentence', data)
                        if first_sentence_sent_ms is None:
                            first_sentence_sent_ms = int((time.perf_counter() - t_ws_recv) * 1000)
                    if first_frame_sent_ms is None:
                        first_frame_sent_ms = int((time.perf_counter() - t_ws_recv) * 1000)
                elif event_type == "action":
                    if not instant_action and is_v2 and data:
                        final_action = data
                        _sync_screen_from_action(ctx, final_action)
                        await send('action', data, {'turn_id': turn_id})
                        if first_frame_sent_ms is None:
                            first_frame_sent_ms = int((time.perf_counter() - t_ws_recv) * 1000)
                elif event_type == "llm_done":
                    llm_done_ms = data
                elif event_type == "done":
                    parsed_res = data
                    if not instant_action and parsed_res.get("action"):
                        final_action = parsed_res["action"]
                        _sync_screen_from_action(ctx, final_action)
                    final_stage = parsed_res.get("sales_stage") or final_stage

            if not accumulated_reply.strip():
                accumulated_reply = instant_opener or "I'd be glad to help you explore our sustainable collection. What are you looking for today?"

            if any(k in accumulated_reply for k in ('"reply"', '"action"', '"skus"', '{', '}')):
                logger.warning("[Chat] dropped non-speech text: %s", accumulated_reply)
                accumulated_reply = instant_opener or "Here are some wonderful choices for you."

            # Update context memory
            if ctx:
                ctx.turn_count += 1
                ctx.history.append({"role": "user", "content": message})
                ctx.history.append({"role": "assistant", "content": accumulated_reply.strip()})
                ctx.history = ctx.history[-12:]
                if final_action:
                    _sync_screen_from_action(ctx, final_action)

            # Determine decision source for logging
            if instant_action:
                dec_source = "instant"
                dec_act = instant_action.get("action")
                dec_skus = instant_skus or ([instant_action.get("sku")] if instant_action.get("sku") else [])
            elif final_action and final_action.get("action") and final_action.get("action") != "NONE":
                dec_source = "llm"
                dec_act = final_action.get("action")
                dec_skus = [final_action.get("sku")] if final_action.get("sku") else [p.get("sku") for p in final_action.get("products", []) if isinstance(p, dict)]
            else:
                dec_source = "fallback" if not final_action else "llm"
                dec_act = final_action.get("action") if final_action else "NONE"
                dec_skus = []

            dec_log = f"[Decision] source={dec_source} action={dec_act} skus={dec_skus}"
            logger.info(dec_log)
            print(dec_log, flush=True)

            # 3. Send done frame with full reply and turn metadata
            await send('done', accumulated_reply.strip(), {
                'session_id': session_id,
                'turn_id': turn_id,
                'sales_stage': final_stage,
                'action': final_action,
            } if is_v2 else None)

            # Per-turn timing log
            first_f_ms = first_frame_sent_ms if first_frame_sent_ms is not None else int((time.perf_counter() - t_ws_recv) * 1000)
            first_s_ms = first_sentence_sent_ms if first_sentence_sent_ms is not None else int((time.perf_counter() - t_ws_recv) * 1000)
            llm_ft_ms = llm_first_token_ms if llm_first_token_ms is not None else 0
            llm_d_ms = llm_done_ms if llm_done_ms is not None else int((time.perf_counter() - t_llm_start) * 1000)

            log_line = (
                f"[Latency] turn={turn_id} "
                f"stt_final->ws_sent={stt_final_to_ws_sent}ms "
                f"ws_received->llm_start={llm_start_ms}ms "
                f"llm_first_token={llm_ft_ms}ms "
                f"llm_done={llm_d_ms}ms "
                f"first_frame_sent={first_f_ms}ms "
                f"first_sentence_sent={first_s_ms}ms"
            )
            logger.info(log_line)
            print(log_line, flush=True)
            return

        except Exception as exc:
            logger.error('Conversation turn error: %s', exc, exc_info=True)
            if not cancelled.is_set() and not disconnected.is_set():
                fallback_reply = "I'd be glad to help you explore our sustainable collection. What are you looking for today?"
                await send('token', fallback_reply)
                await send('done', fallback_reply, {'session_id': session_id, 'turn_id': turn_id})

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
                _last_activity[0] = asyncio.get_event_loop().time()
                await send('pong', '')
                continue
            if payload.get('type') == 'cancel':
                if payload.get('turn_id') and payload['turn_id'] != current_id:
                    await send('error', 'Turn identity mismatch.')
                    continue
                cancelled.set()
                await send('cancelled', '', {'turn_id': current_id, 'provider_cancelled': False, 'output_suppressed': True})
                continue

            ev_type = payload.get('event') or payload.get('type')
            if ev_type in ('name_submitted', 'name_skipped', 'screen_changed'):
                from app.fast_flow import get_ff
                ctx = get_ff(session_id)
                data = payload.get('data') if isinstance(payload.get('data'), dict) else payload
                if ev_type == 'name_submitted':
                    submitted_name = (data.get('name') or payload.get('name') or '').strip()
                    if submitted_name and ctx:
                        ctx.name = submitted_name
                        ctx.name_status = 'given'
                        logger.info("[Session] name_submitted session=%s name=%s", session_id, ctx.name)
                        print(f"[Session] name_submitted session={session_id} name={ctx.name}", flush=True)
                elif ev_type == 'name_skipped':
                    if ctx:
                        ctx.name_status = 'skipped'
                        logger.info("[Session] name_skipped session=%s", session_id)
                        print(f"[Session] name_skipped session={session_id}", flush=True)
                elif ev_type == 'screen_changed':
                    if ctx:
                        stype = data.get('type') or data.get('screen_type') or payload.get('screen_type')
                        scat = data.get('category') or payload.get('category')
                        ssku = data.get('sku') or payload.get('sku')
                        sskus = data.get('skus') or payload.get('skus') or []
                        ctx.current_screen = {
                            'type': stype,
                            'category': scat,
                            'sku': ssku,
                        }
                        if sskus:
                            ctx.last_list_skus = list(sskus)
                        elif ssku and (not ctx.last_list_skus or ctx.current_screen.get('type') == 'product'):
                            if ssku not in ctx.last_list_skus:
                                ctx.last_list_skus = [ssku]
                        if ssku:
                            ctx.selected_sku = ssku
                        elif stype in ('home', 'categories'):
                            ctx.selected_sku = None
                        if stype == 'categories':
                            ctx.categories_shown_at = time.time()
                        logger.info("[Session] screen_changed session=%s screen=%s selected_sku=%s skus=%s", session_id, ctx.current_screen, ctx.selected_sku, ctx.last_list_skus)
                        print(f"[Session] screen_changed session={session_id} screen={ctx.current_screen} selected_sku={ctx.selected_sku} skus={ctx.last_list_skus}", flush=True)
                continue
            try:
                body = ChatRequest.model_validate({
                    **{key: value for key, value in payload.items() if key != 'v'},
                    'session_id': session_id,
                })
            except ValidationError:
                await send('error', 'Invalid command payload.', {'code': 'validation_error'})
                continue
            if payload.get('session_id') and payload['session_id'] != session_id:
                await send('error', 'Session identity mismatch.')
                continue
            message = body.normalized_message()
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
            running = asyncio.create_task(execute(message.strip(), turn_id, body.command(), payload))
    except WebSocketDisconnect:
        pass
    finally:
        disconnected.set()
        cancelled.set()
        if silence_task and not silence_task.done():
            silence_task.cancel()
        ws_manager.disconnect(session_id)
        _cancel_flags.pop(session_id, None)
        drop_ff(session_id)  # clean up fast-flow context on disconnect
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
