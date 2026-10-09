"""Unit and integration tests for Greeny AI MongoDB persistence and schemas.

Tests:
- Step 4: Save conversation, Continue conversation, Restart recovery, Cross-kiosk denial, Clear session.
- Step 5: Schemas for kiosk_sessions, messages, customer_consents, sales_leads, quote_drafts,
          unanswered_questions, approved_knowledge, phone masking, consent enforcement.
- Step 3: Health check reporting and index definitions with 90-day retention.
"""

from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from app.schemas_mongo import (
    ApprovedKnowledgeModel,
    CustomerConsentModel,
    KioskSessionModel,
    MessageModel,
    QuoteDraftModel,
    SalesLeadModel,
    UnansweredQuestionModel,
    mask_phone_number,
)
from app.mongo_checkpointer import CrossKioskAccessError, MongoCheckpointSaver


# ==============================================================================
# In-Memory Mock Mongo Database for Unit Testing
# ==============================================================================

class MockCollection:
    def __init__(self, name: str):
        self.name = name
        self.docs = []
        self.indexes = []

    def create_index(self, keys, **kwargs):
        self.indexes.append({"keys": keys, "kwargs": kwargs})

    def insert_one(self, doc):
        d = dict(doc)
        if "_id" not in d:
            d["_id"] = str(len(self.docs) + 1)
        from pymongo.errors import DuplicateKeyError
        if any(existing.get('_id') == d['_id'] for existing in self.docs):
            raise DuplicateKeyError('duplicate key')
        self.docs.append(d)
        class Res:
            inserted_id = d["_id"]
        return Res()

    def update_one(self, filter_q, update_doc, upsert=False):
        doc = self.find_one(filter_q)
        exists = doc is not None
        doc = doc or dict(filter_q)
        if not exists and not upsert:
            return
        if not exists:
            doc.update(update_doc.get('$setOnInsert', {}))
        doc.update(update_doc.get('$set', {}))
        for key, value in update_doc.get('$inc', {}).items():
            doc[key] = doc.get(key, 0) + value
        self.replace_one(filter_q, doc, upsert=True)

    def find_one_and_update(self, filter_q, update_doc, upsert=False, return_document=None):
        from pymongo import ReturnDocument
        doc = self.find_one(filter_q)
        exists = doc is not None
        if not exists and not upsert:
            return None
        original_doc = dict(doc) if exists else None
        doc = doc or dict(filter_q)
        if not exists:
            doc.update(update_doc.get('$setOnInsert', {}))
        doc.update(update_doc.get('$set', {}))
        for key, value in update_doc.get('$inc', {}).items():
            doc[key] = doc.get(key, 0) + value
        self.replace_one(filter_q, doc, upsert=True)
        if return_document == ReturnDocument.AFTER:
            return dict(doc)
        return original_doc

    def replace_one(self, filter_q, replacement, upsert=False):
        for idx, doc in enumerate(self.docs):
            if all(doc.get(k) == v for k, v in filter_q.items()):
                self.docs[idx] = dict(replacement)
                return
        if upsert:
            self.docs.append(dict(replacement))

    def _matches(self, doc, filter_q):
        for k, v in filter_q.items():
            if isinstance(v, dict):
                for op, val in v.items():
                    doc_val = doc.get(k)
                    if op == '$lt' and not (doc_val is not None and doc_val < val):
                        return False
                    elif op == '$gt' and not (doc_val is not None and doc_val > val):
                        return False
                    elif op == '$lte' and not (doc_val is not None and doc_val <= val):
                        return False
                    elif op == '$gte' and not (doc_val is not None and doc_val >= val):
                        return False
                    elif op == '$ne' and doc_val == val:
                        return False
                    elif op == '$in' and doc_val not in val:
                        return False
            else:
                if doc.get(k) != v:
                    return False
        return True

    def find_one(self, filter_q, projection=None, sort=None):
        matched = [
            d for d in self.docs
            if self._matches(d, filter_q)
        ]
        if not matched:
            return None
        if sort:
            key, direction = sort[0]
            matched = sorted(matched, key=lambda x: x.get(key, ""), reverse=(direction < 0))
        doc = matched[0]
        if projection:
            return {k: doc[k] for k in projection if k in doc}
        return dict(doc)

    def find(self, filter_q):
        matched = [
            dict(d) for d in self.docs
            if self._matches(d, filter_q)
        ]
        class Cursor:
            def __init__(self, items):
                self.items = items
            def sort(self, sort_list):
                key, direction = sort_list[0]
                self.items = sorted(self.items, key=lambda x: x.get(key, 0), reverse=(direction < 0))
                return self
            def limit(self, n):
                self.items = self.items[:n]
                return self
            def __iter__(self):
                return iter(self.items)
        return Cursor(matched)

    def delete_many(self, filter_q):
        initial_len = len(self.docs)
        self.docs = [
            d for d in self.docs
            if not self._matches(d, filter_q)
        ]
        class DelRes:
            deleted_count = initial_len - len(self.docs)
        return DelRes()


class MockDatabase:
    def __init__(self, name: str = "greeny_ai"):
        self.name = name
        self.collections = {}

    def __getitem__(self, name: str) -> MockCollection:
        if name not in self.collections:
            self.collections[name] = MockCollection(name)
        return self.collections[name]

    def __getattr__(self, name: str) -> MockCollection:
        return self[name]

    def command(self, cmd):
        return {"ok": 1.0}


# ==============================================================================
# STEP 5: SCHEMA TESTS
# ==============================================================================

def test_kiosk_session_schema():
    session = KioskSessionModel(session_id="sess-001", kiosk_id="kiosk-south-01")
    assert session.session_id == "sess-001"
    assert session.kiosk_id == "kiosk-south-01"
    assert session.sales_stage == "GREETING"
    assert session.created_at is not None


def test_customer_consent_phone_masking():
    raw_phone = "+919876543210"
    masked = mask_phone_number(raw_phone)
    assert masked == "+919****3210"
    assert "9876543210" not in masked

    consent = CustomerConsentModel(
        consent_id="consent-01",
        session_id="sess-001",
        kiosk_id="kiosk-south-01",
        phone_masked=raw_phone,
        consent_type="whatsapp_promotional",
        granted=True,
    )
    assert consent.phone_masked == masked


def test_sales_lead_requires_explicit_consent():
    # If phone is provided without verified consent, validation must fail
    with pytest.raises(ValueError, match="verified consent"):
        SalesLeadModel(
            lead_id="lead-01",
            session_id="sess-001",
            kiosk_id="kiosk-01",
            intent_summary="Wants 50 copper bottles for corporate gifting",
            contact_phone="+919876543210",
            consent_verified=False,
        )

    # Valid lead with explicit consent
    lead = SalesLeadModel(
        lead_id="lead-01",
        session_id="sess-001",
        kiosk_id="kiosk-01",
        intent_summary="Wants 50 copper bottles for corporate gifting",
        product_skus=["GF:GFB-001"],
        contact_phone="+919876543210",
        consent_verified=True,
    )
    assert lead.contact_phone == "+919876543210"
    assert lead.consent_verified is True


def test_quote_draft_and_knowledge_schemas():
    from decimal import Decimal
    from app.schemas_mongo import QuoteItem

    item = QuoteItem(
        sku="GF:GFB-001",
        name="Bamboo Bottle 750ml",
        quantity=50,
        unit_price_estimate=Decimal("500.00"),
    )
    draft = QuoteDraftModel(
        quote_id="quote-101",
        session_id="sess-001",
        kiosk_id="kiosk-01",
        items=[item],
        total_estimate=Decimal("25000.00"),
    )
    assert draft.quote_id == "quote-101"
    assert draft.total_estimate == Decimal("25000.00")

    qa = UnansweredQuestionModel(
        id="uq-001",
        session_id="sess-001",
        kiosk_id="kiosk-01",
        user_query="Can I laser engrave my corporate logo in red color?",
        fallback_reason="No evidence contract for color engraving",
    )
    assert qa.user_query.startswith("Can I")

    kb = ApprovedKnowledgeModel(
        doc_id="doc-laser-01",
        title="Laser Engraving Guidelines",
        category="customization",
        source_file="guidelines.md",
        content_sha256="a" * 64,
        approved_by="store_manager_01",
    )
    assert kb.doc_id == "doc-laser-01"


# ==============================================================================
# STEP 4: PERSISTENT LANGGRAPH CHECKPOINTER TESTS
# ==============================================================================

def test_mongo_checkpointer_save_and_restore():
    mock_db = MockDatabase("greeny_ai")
    saver1 = MongoCheckpointSaver(mock_db)
    saver1.ensure_indexes()

    thread_id = "test-thread-101"
    kiosk_id = "kiosk-01"
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "kiosk_id": kiosk_id}}

    cp = {
        "v": 1,
        "id": "cp-1",
        "ts": "2026-10-08T12:00:00Z",
        "channel_versions": {"messages": 1},
        "channel_values": {"messages": ["Hello Greeny!"]},
        "versions_seen": {},
    }
    meta = {"source": "input", "step": 1, "writes": {}, "parents": {}}

    # 1. Save checkpoint
    saved_config = saver1.put(config, cp, meta, {"messages": 1})
    assert saved_config["configurable"]["checkpoint_id"] == "cp-1"

    # 2. Simulate Backend Restart (new checkpointer instance on same DB)
    saver2 = MongoCheckpointSaver(mock_db)
    restored = saver2.get_tuple(config)

    assert restored is not None
    assert restored.checkpoint["id"] == "cp-1"
    assert restored.checkpoint["channel_values"]["messages"] == ["Hello Greeny!"]


def test_mongo_checkpointer_continue_conversation():
    mock_db = MockDatabase("greeny_ai")
    saver = MongoCheckpointSaver(mock_db)

    thread_id = "test-thread-102"
    kiosk_id = "kiosk-01"
    config1 = {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "kiosk_id": kiosk_id}}

    cp1 = {
        "v": 1, "id": "cp-1", "ts": "2026-10-08T12:00:00Z",
        "channel_versions": {"messages": 1}, "channel_values": {"messages": ["Hi"]}, "versions_seen": {},
    }
    saver.put(config1, cp1, {"step": 1}, {"messages": 1})

    # Continue conversation with turn 2
    config2 = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_ns": "",
            "checkpoint_id": "cp-1",
            "kiosk_id": kiosk_id,
        }
    }
    cp2 = {
        "v": 1, "id": "cp-2", "ts": "2026-10-08T12:01:00Z",
        "channel_versions": {"messages": 2}, "channel_values": {"messages": ["Hi", "Recommend bamboo bottle"]}, "versions_seen": {},
    }
    saver.put(config2, cp2, {"step": 2}, {"messages": 2})

    latest = saver.get_tuple(config1)
    assert latest is not None
    assert latest.checkpoint["id"] == "cp-2"
    assert len(latest.checkpoint["channel_values"]["messages"]) == 2


def test_mongo_checkpointer_cross_kiosk_denial():
    mock_db = MockDatabase("greeny_ai")
    saver = MongoCheckpointSaver(mock_db)

    thread_id = "test-thread-secure"
    owner_kiosk = "kiosk-authorized-01"
    rogue_kiosk = "kiosk-unauthorized-02"

    config_owner = {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "kiosk_id": owner_kiosk}}
    cp = {
        "v": 1, "id": "cp-1", "ts": "2026-10-08T12:00:00Z",
        "channel_versions": {"messages": 1}, "channel_values": {"messages": ["Secret message"]}, "versions_seen": {},
    }
    saver.put(config_owner, cp, {"step": 1}, {"messages": 1})

    # Unauthorized kiosk attempts read
    config_rogue = {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "kiosk_id": rogue_kiosk}}
    with pytest.raises(CrossKioskAccessError, match="ownership mismatch"):
        saver.get_tuple(config_rogue)

    # Unauthorized kiosk attempts delete
    with pytest.raises(CrossKioskAccessError, match="ownership mismatch"):
        saver.delete_thread(thread_id, request_kiosk_id=rogue_kiosk)


def test_mongo_checkpointer_clear_session():
    mock_db = MockDatabase("greeny_ai")
    saver = MongoCheckpointSaver(mock_db)

    thread_id = "test-thread-clear"
    kiosk_id = "kiosk-01"
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "kiosk_id": kiosk_id}}

    cp = {
        "v": 1, "id": "cp-1", "ts": "2026-10-08T12:00:00Z",
        "channel_versions": {"messages": 1}, "channel_values": {"messages": ["Hello"]}, "versions_seen": {},
    }
    saver.put(config, cp, {"step": 1}, {"messages": 1})
    assert saver.get_tuple(config) is not None

    # Clear session by authorized kiosk
    saver.delete_thread(thread_id, request_kiosk_id=kiosk_id)
    assert saver.get_tuple(config) is None


def test_mongo_health_check_disabled(monkeypatch):
    from app.database import check_mongo_health
    from app.config import settings

    monkeypatch.setattr(settings, "greeny_ai_mongo_url", None)
    result = check_mongo_health()
    assert result["status"] == "disabled"
    assert "not configured" in result["message"]


def test_greeny_ai_indexes_creation():
    from app.database import create_indexes_for_greeny_ai

    mock_db = MockDatabase("greeny_ai")
    create_indexes_for_greeny_ai(mock_db)

    # Verify TTL indexes exist on sessions and messages
    session_indexes = mock_db["kiosk_sessions"].indexes
    assert any(idx["kwargs"].get("expireAfterSeconds") == 90 * 86400 for idx in session_indexes)
    assert any(idx["kwargs"].get("unique") is True for idx in session_indexes)

    message_indexes = mock_db["messages"].indexes
    assert any(idx["kwargs"].get("expireAfterSeconds") == 90 * 86400 for idx in message_indexes)

    # Verify TTL & uniqueness indexes on coordinator collections
    ticket_indexes = mock_db["backend_tickets"].indexes
    assert any(idx["kwargs"].get("expireAfterSeconds") == 0 for idx in ticket_indexes)
    assert any(idx["kwargs"].get("unique") is True for idx in ticket_indexes)

    rate_indexes = mock_db["backend_rates"].indexes
    assert any(idx["kwargs"].get("expireAfterSeconds") == 0 for idx in rate_indexes)

    mutex_indexes = mock_db["backend_mutexes"].indexes
    assert any(idx["kwargs"].get("unique") is True for idx in mutex_indexes)
