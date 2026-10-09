"""Operational MongoDB Storage Manager for Greeny AI Kiosk.

Handles real database writes to the greeny_ai database for:
- kiosk_sessions
- messages
- customer_consents
- sales_leads
- unanswered_questions
- approved_knowledge

Enforces consent before storing customer contact info.
Fails safely when MongoDB is down in production.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional
from uuid import uuid4, uuid5, NAMESPACE_URL
from functools import wraps
from app.privacy import redact_contacts
from app.mongo_checkpointer import MongoCheckpointSaver
from app.coordination import Coordinator
from pymongo.errors import DuplicateKeyError

from pymongo.database import Database
from pymongo.errors import PyMongoError

from app.config import settings
from app.database import get_greeny_ai_db
from app.schemas_mongo import (
    CustomerConsentModel,
    KioskSessionModel,
    MessageModel,
    SalesLeadModel,
    UnansweredQuestionModel,
    mask_phone_number,
)

logger = logging.getLogger("greenie.storage")


class DatabaseConnectionError(Exception):
    """Raised when MongoDB is unreachable in production mode."""


def get_operational_db() -> Optional[Database]:
    """Retrieve the operational greeny_ai database or raise in production."""
    try:
        db = get_greeny_ai_db()
        if db is None and settings.checkpointer_backend == "mongodb":
            raise DatabaseConnectionError("Database persistence unavailable.")
        return db
    except Exception as e:
        if settings.checkpointer_backend == "mongodb":
            logger.error("Production MongoDB outage detected: %s", type(e).__name__)
            raise DatabaseConnectionError("Database persistence unavailable.") from e
        logger.debug("Operational DB unavailable in non-production mode: %s", type(e).__name__)
        return None


def record_conversation_turn(
    session_id: str,
    kiosk_id: str,
    user_message: str,
    ai_reply: str,
    sales_stage: str = "GREETING",
    customer_name: Optional[str] = None,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    unanswered_reason: Optional[str] = None,
    db: Optional[Database] = None,
) -> bool:
    """Automatically persist session metadata and conversation messages to MongoDB."""
    target_db = db if db is not None else get_operational_db()
    if target_db is None:
        return False

    MongoCheckpointSaver(target_db).claim(session_id, kiosk_id)
    now = datetime.now(timezone.utc)
    try:
        # 1. Update/Upsert Session in kiosk_sessions
        session_update: Dict[str, Any] = {
            "session_id": session_id,
            "kiosk_id": kiosk_id,
            "sales_stage": sales_stage,
            "last_active_at": now,
        }
        if customer_name:
            session_update["customer_name"] = "[REDACTED]"

        target_db.kiosk_sessions.update_one(
            {"session_id": session_id, "kiosk_id": kiosk_id},
            {
                "$set": session_update,
                "$inc": {"turn_count": 1},
                "$setOnInsert": {"created_at": now, "language_preference": "en"},
            },
            upsert=True,
        )

        # 2. Insert User Message in messages
        target_db.messages.insert_one({
            "message_id": uuid4().hex,
            "session_id": session_id,
            "kiosk_id": kiosk_id,
            "role": "human",
            "content": redact_contacts(user_message),
            "created_at": now,
        })

        # 3. Insert Assistant Message in messages
        target_db.messages.insert_one({
            "message_id": uuid4().hex,
            "session_id": session_id,
            "kiosk_id": kiosk_id,
            "role": "ai",
            "content": redact_contacts(ai_reply),
            "tool_calls": redact_contacts(tool_calls or []),
            "created_at": now,
        })

        # 4. Record Unanswered Question if flagged
        if unanswered_reason:
            target_db.unanswered_questions.insert_one({
                "id": uuid4().hex,
                "session_id": session_id,
                "kiosk_id": kiosk_id,
                "user_query": redact_contacts(user_message),
                "fallback_reason": redact_contacts(unanswered_reason),
                "timestamp": now,
            })

        return True
    except PyMongoError as e:
        logger.error("Failed to write conversation turn to MongoDB: %s", type(e).__name__)
        if settings.checkpointer_backend == "mongodb":
            raise DatabaseConnectionError("Database persistence unavailable.") from e
        return False


def record_customer_consent(
    session_id: str,
    kiosk_id: str,
    raw_phone: str,
    consent_type: str = "quotation_contact_only",
    granted: bool = False,
    db: Optional[Database] = None,
    operation_id: Optional[str] = None,
    status: Optional[str] = None,
) -> Optional[str]:
    """Persist customer consent record with masked phone number."""
    target_db = db if db is not None else get_operational_db()
    if target_db is None:
        return None

    MongoCheckpointSaver(target_db).claim(session_id, kiosk_id)
    status = status or ('granted' if granted else 'declined')
    if status not in ('granted', 'declined', 'revoked') or granted != (status == 'granted'):
        raise ValueError('Invalid consent status.')
    consent_id = uuid5(NAMESPACE_URL, f"{kiosk_id}:{session_id}:{consent_type}").hex
    masked = mask_phone_number(raw_phone)
    now = datetime.now(timezone.utc)

    doc = {
        "consent_id": consent_id,
        "session_id": session_id,
        "kiosk_id": kiosk_id,
        "consent_type": consent_type,
        "granted": granted,
        "status": status,
        "phone_masked": masked,
        "terms_version": "v1.0",
        "timestamp": now,
    }
    try:
        with Coordinator(target_db).exclusive('consent:' + session_id):
            operation_key = uuid5(NAMESPACE_URL, f'{consent_id}:{operation_id or status}').hex
            if target_db['consent_operations'].find_one({'_id': operation_key}):
                return consent_id
            target_db.customer_consents.replace_one({'_id': consent_id}, {'_id': consent_id, **doc}, upsert=True)
            if not granted:
                # Remove any draft contact association on refusal/revocation.
                target_db.sales_leads.delete_many({'session_id': session_id, 'kiosk_id': kiosk_id})
            target_db['consent_operations'].insert_one({'_id': operation_key, 'created_at': now})
        return consent_id
    except PyMongoError as e:
        logger.error("Failed to write consent to MongoDB: %s", type(e).__name__)
        if settings.checkpointer_backend == "mongodb":
            raise DatabaseConnectionError("Database persistence unavailable.") from e
        return None


def serialized_lead(fn):
    @wraps(fn)
    def call(session_id, kiosk_id, *args, **kwargs):
        target = kwargs.get('db')
        if target is None:
            target = get_operational_db()
        if target is None:
            return fn(session_id, kiosk_id, *args, **kwargs)
        with Coordinator(target).exclusive('consent:' + session_id):
            return fn(session_id, kiosk_id, *args, **kwargs)
    return call


@serialized_lead
def record_sales_lead(
    session_id: str,
    kiosk_id: str,
    intent_summary: str,
    product_skus: List[str],
    quantity: Optional[int] = None,
    budget_inr: Optional[float] = None,
    contact_phone: Optional[str] = None,
    consent_verified: bool = False,
    db: Optional[Database] = None,
) -> Optional[str]:
    """Store qualified B2B sales lead. Strictly enforces consent before storing phone."""
    if contact_phone and not consent_verified:
        raise ValueError("Cannot store customer phone number without verified consent.")

    target_db = db if db is not None else get_operational_db()
    if target_db is None:
        return None

    MongoCheckpointSaver(target_db).claim(session_id, kiosk_id)
    if contact_phone:
        consent = target_db.customer_consents.find_one({
            "session_id": session_id, "kiosk_id": kiosk_id,
            "consent_type": "quotation_contact_only", "granted": True,
        })
        consent_time = consent.get('timestamp') if consent else None
        expired = not consent_time or (datetime.now(timezone.utc) - consent_time.replace(tzinfo=timezone.utc)).total_seconds() > settings.greeny_ai_retention_days * 86400
        if not consent or expired:
            raise ValueError("Stored quotation consent is required.")
    lead_id = "LD-" + uuid5(NAMESPACE_URL, f"{kiosk_id}:{session_id}:quote").hex
    now = datetime.now(timezone.utc)

    doc = {
        "lead_id": lead_id,
        "session_id": session_id,
        "kiosk_id": kiosk_id,
        "lead_type": "b2b",
        "intent_summary": redact_contacts(intent_summary),
        "product_skus": product_skus,
        "quantity": quantity,
        "budget_inr": budget_inr,
        "consent_verified": consent_verified,
        "contact_phone": mask_phone_number(contact_phone) if contact_phone else None,
        "status": "draft_not_submitted",
        "created_at": now,
    }
    try:
        target_db.sales_leads.replace_one({"_id": lead_id}, {"_id": lead_id, **doc}, upsert=True)
        return lead_id
    except PyMongoError as e:
        logger.error("Failed to write sales lead to MongoDB: %s", type(e).__name__)
        if settings.checkpointer_backend == "mongodb":
            raise DatabaseConnectionError("Database persistence unavailable.") from e
        return None


def record_approved_knowledge(
    doc_id: str,
    title: str,
    category: str,
    source_file: str,
    content_sha256: str,
    approved_by: str,
    sku_scope: Optional[List[str]] = None,
    db: Optional[Database] = None,
) -> bool:
    """Store verified knowledge article in approved_knowledge collection."""
    target_db = db if db is not None else get_operational_db()
    if target_db is None:
        return False

    now = datetime.now(timezone.utc)
    doc = {
        "doc_id": doc_id,
        "title": title,
        "category": category,
        "source_file": source_file,
        "content_sha256": content_sha256,
        "approved_by": approved_by,
        "approved_at": now,
        "sku_scope": sku_scope or [],
    }
    try:
        target_db.approved_knowledge.replace_one({"doc_id": doc_id}, doc, upsert=True)
        return True
    except PyMongoError as e:
        logger.error("Failed to write approved knowledge: %s", type(e).__name__)
        if settings.checkpointer_backend == "mongodb":
            raise DatabaseConnectionError("Database persistence unavailable.") from e
        return False
