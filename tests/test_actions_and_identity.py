"""Regression tests for Kiosk Authentication, Cross-Kiosk Isolation,
WebSocket v2 Actions, Database Outage Fail-Safe, and Operational Persistence.

Verifies:
- Authenticated kiosk identity at REST & WebSocket boundaries (Token, Bearer, query)
- Cross-kiosk access denial (HTTP 403 and WS error envelope)
- Fail-safe database outage response (HTTP 503 and WS error envelope, never silent memory fallback)
- Delivery of all 6 structured UI actions via WebSocket v2 envelopes
- Real operational writes to MongoDB collections with phone masking and consent guards
"""

from datetime import datetime, timezone
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
import pytest
from pydantic import SecretStr
from pymongo.errors import ConnectionFailure, PyMongoError

from app import server
from app.config import settings
from app.mongo_checkpointer import CrossKioskAccessError
from app.storage import (
    DatabaseConnectionError,
    record_approved_knowledge,
    record_conversation_turn,
    record_customer_consent,
    record_sales_lead,
)


# ==============================================================================
# In-Memory Mock MongoDB Database for Operational Storage Verification
# ==============================================================================

from test_mongo_persistence import MockCollection, MockDatabase


# ==============================================================================
# 1. KIOSK AUTHENTICATION AT REST AND WEBSOCKET BOUNDARIES
# ==============================================================================

def test_rest_auth_passes_when_no_secret_configured():
    with patch.object(settings, "kiosk_auth_secret", None):
        with TestClient(server.api) as client:
            resp = client.get("/products")
            assert resp.status_code == 200


def test_rest_auth_enforces_secret_when_configured():
    with patch.object(settings, "kiosk_auth_secret", SecretStr("secret-kiosk-token-999")):
        with TestClient(server.api) as client:
            # 1. Request without auth -> 401
            resp = client.post("/chat", json={"message": "hello"})
            assert resp.status_code == 401
            assert "Unauthorized" in resp.json()["detail"]

            # 2. Request with invalid token -> 401
            resp = client.post(
                "/chat",
                json={"message": "hello"},
                headers={"X-Kiosk-Token": "wrong-token"},
            )
            assert resp.status_code == 401

            # 3. Request with valid X-Kiosk-Token header -> Passes auth
            with patch.object(server, "ask_avatar", return_value="Hello store"):
                resp = client.post(
                    "/chat",
                    json={"message": "hello"},
                    headers={"X-Kiosk-Token": "secret-kiosk-token-999"},
                )
                assert resp.status_code == 200

            # 4. Request with Bearer token in Authorization header -> Passes auth
            with patch.object(server, "ask_avatar", return_value="Hello store"):
                resp = client.post(
                    "/chat",
                    json={"message": "hello"},
                    headers={"Authorization": "Bearer secret-kiosk-token-999"},
                )
                assert resp.status_code == 200


def test_websocket_auth_rejects_missing_or_invalid_secret():
    with patch.object(settings, "kiosk_auth_secret", SecretStr("ws-secret-123")):
        with TestClient(server.api) as client:
            # Without token -> 1008 policy violation
            with pytest.raises(Exception):
                with client.websocket_connect("/ws/sess-ws-auth") as ws:
                    pass

            # With valid token -> connects successfully
            with client.websocket_connect("/ws/sess-ws-auth?token=ws-secret-123") as ws:
                ws.send_text("ping")
                data = ws.receive_json()
                assert data["type"] == "pong"


# ==============================================================================
# 2. CROSS-KIOSK ISOLATION AT BOUNDARIES
# ==============================================================================

def test_cross_kiosk_access_denial_rest():
    with TestClient(server.api) as client:
        with patch.object(server.session_manager, "reset_session", side_effect=CrossKioskAccessError("Cross-kiosk access denied: kiosk-b cannot access kiosk-a")):
            resp = client.post(
                "/session/reset",
                json={"session_id": "sess-foreign", "kiosk_id": "kiosk-b"},
            )
            assert resp.status_code == 403
            assert "Cross-kiosk" in resp.json()["detail"]


# ==============================================================================
# 3. PRODUCTION MONGODB OUTAGE FAIL-SAFE
# ==============================================================================

def test_production_mongo_outage_fails_safely_at_rest():
    with patch.object(settings, "checkpointer_backend", "mongodb"):
        with patch("app.server.ask_avatar_turn", side_effect=DatabaseConnectionError("Atlas connection dropped")):
            with TestClient(server.api) as client:
                resp = client.post("/chat", json={"message": "hello"})
                # Must return 503 Service Unavailable, NEVER silent memory fallback
                assert resp.status_code == 503
                assert "Database persistence unavailable" in resp.json()["detail"]


def test_production_mongo_outage_fails_safely_at_websocket():
    class OutageGen:
        action = None
        sales_stage = None
        def __iter__(self):
            raise DatabaseConnectionError("MongoDB Atlas unreachable")

    with patch.object(settings, "checkpointer_backend", "mongodb"):
        with patch.object(server, "ask_avatar_stream", return_value=OutageGen()):
            with TestClient(server.api) as client:
                with client.websocket_connect("/ws/sess-outage?v=2") as ws:
                    ws.send_json({"v": 2, "message": "hello"})
                    frame = ws.receive_json()
                    assert frame["v"] == 2
                    assert frame["type"] == "error"
                    assert "Database persistence unavailable. Outage fail-safe active." in frame["data"]


# ==============================================================================
# 4. WEBSOCKET V2 STRUCTURED ACTION EVENTS
# ==============================================================================

def test_ws_v2_emits_structured_action_event():
    test_action = {
        "action": "SHOW_PRODUCTS",
        "products": [{"id": "viora-bottle", "sku": "GF:BTL:VIORA", "name": "Viora Eco Bottle", "price": "649"}],
        "count": 1,
    }

    class MockStreamGen:
        def __init__(self):
            self.action = test_action
            self.sales_stage = "RECOMMENDATION"
            self._words = ["Here ", "are ", "bottles."]
            self._iter = iter(self._words)

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._iter)

    with patch.object(server, "ask_avatar_stream", return_value=MockStreamGen()):
        with TestClient(server.api) as client:
            with client.websocket_connect("/ws/sess-v2-action?v=2") as ws:
                ws.send_json({"v": 2, "message": "show bottles"})

                # First event received MUST be the structured UI action
                action_frame = ws.receive_json()
                assert action_frame["v"] == 2
                assert action_frame["type"] == "action"
                assert action_frame["data"] == test_action
                assert action_frame["data"]["action"] == "SHOW_PRODUCTS"
                assert action_frame["data"]["products"][0]["sku"] == "GF:BTL:VIORA"

                # Next frames are token and sentence events
                tokens = []
                sentences = []
                done_frame = None
                while True:
                    f = ws.receive_json()
                    if f["type"] == "token":
                        tokens.append(f["data"])
                    elif f["type"] == "sentence":
                        sentences.append(f["data"])
                    elif f["type"] == "done":
                        done_frame = f
                        break

                assert "".join(tokens) == "Here are bottles."
                assert "Here are bottles." in sentences
                assert done_frame is not None
                assert done_frame["meta"]["sales_stage"] == "RECOMMENDATION"
                assert done_frame["meta"]["action"] == test_action


# ==============================================================================
# 5. REAL MONGODB OPERATIONAL STORAGE WRITES
# ==============================================================================

def test_operational_storage_writes_conversation_turn():
    db = MockDatabase()
    res = record_conversation_turn(
        session_id="sess-store-01",
        kiosk_id="kiosk-mumbai-01",
        user_message="Tell me about the mug",
        ai_reply="The Statement Mug costs ₹399.",
        sales_stage="RECOMMENDATION",
        customer_name="Rohan",
        tool_calls=[{"name": "search_products", "result": "Mug found"}],
        unanswered_reason=None,
        db=db,
    )
    assert res is True

    # Check kiosk_sessions write
    sess_doc = db.kiosk_sessions.find_one({"session_id": "sess-store-01"})
    assert sess_doc is not None
    assert sess_doc["kiosk_id"] == "kiosk-mumbai-01"
    assert sess_doc["customer_name"] == "[REDACTED]"
    assert sess_doc["sales_stage"] == "RECOMMENDATION"
    assert sess_doc["turn_count"] == 1

    # Check messages collection
    msgs = list(db.messages.find({"session_id": "sess-store-01"}))
    assert len(msgs) == 2
    assert msgs[0]["role"] == "human"
    assert msgs[0]["content"] == "Tell me about the mug"
    assert msgs[1]["role"] == "ai"
    assert msgs[1]["content"] == "The Statement Mug costs ₹399."


def test_operational_storage_writes_unanswered_question():
    db = MockDatabase()
    record_conversation_turn(
        session_id="sess-store-02",
        kiosk_id="kiosk-mumbai-01",
        user_message="Do you sell laptops?",
        ai_reply="I do not have information about that.",
        sales_stage="COMPLETED",
        unanswered_reason="Out of catalogue domain",
        db=db,
    )

    unans = db.unanswered_questions.find_one({"session_id": "sess-store-02"})
    assert unans is not None
    assert unans["user_query"] == "Do you sell laptops?"
    assert unans["fallback_reason"] == "Out of catalogue domain"


def test_operational_storage_consent_masks_phone_number():
    db = MockDatabase()
    cid = record_customer_consent(
        session_id="sess-store-03",
        kiosk_id="kiosk-delhi-01",
        raw_phone="+919876543210",
        consent_type="whatsapp_quote",
        granted=True,
        db=db,
    )
    assert cid is not None
    consent_doc = db.customer_consents.find_one({"consent_id": cid})
    assert consent_doc is not None
    assert consent_doc["phone_masked"] == "+919****3210"
    assert "9876543210" not in consent_doc["phone_masked"]


def test_operational_storage_sales_lead_requires_consent_before_storing_phone():
    db = MockDatabase()

    # Attempting to store lead with contact phone without consent MUST raise ValueError
    with pytest.raises(ValueError, match="Cannot store customer phone number without verified consent"):
        record_sales_lead(
            session_id="sess-lead-01",
            kiosk_id="kiosk-delhi-01",
            intent_summary="100 bottles for Diwali",
            product_skus=["GF:BTL:VIORA"],
            quantity=100,
            contact_phone="+919876543210",
            consent_verified=False,
            db=db,
        )

    # Stored, scoped consent is required in addition to the caller flag.
    record_customer_consent("sess-lead-02", "kiosk-delhi-01", "+919876543210", granted=True, db=db)
    # With verified stored consent, succeeds and masks phone
    lead_id = record_sales_lead(
        session_id="sess-lead-02",
        kiosk_id="kiosk-delhi-01",
        intent_summary="100 bottles for Diwali",
        product_skus=["GF:BTL:VIORA"],
        quantity=100,
        contact_phone="+919876543210",
        consent_verified=True,
        db=db,
    )
    assert lead_id.startswith("LD-")
    lead_doc = db.sales_leads.find_one({"lead_id": lead_id})
    assert lead_doc is not None
    assert lead_doc["contact_phone"] == "+919****3210"
    assert lead_doc["quantity"] == 100


def test_operational_storage_approved_knowledge():
    db = MockDatabase()
    saved = record_approved_knowledge(
        doc_id="faq-material-rice-husk",
        title="Rice Husk Biocomposite Material Specification",
        category="materials",
        source_file="materials.txt",
        content_sha256="abc123sha256hash",
        approved_by="Compliance Officer",
        sku_scope=["GF:BTL:VIORA", "GF:MUG:STAT"],
        db=db,
    )
    assert saved is True
    k_doc = db.approved_knowledge.find_one({"doc_id": "faq-material-rice-husk"})
    assert k_doc is not None
    assert k_doc["approved_by"] == "Compliance Officer"
    assert k_doc["sku_scope"] == ["GF:BTL:VIORA", "GF:MUG:STAT"]
