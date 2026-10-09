"""MongoDB operational database integration for Greeny AI.

Provides database connectivity, health checks, index creation, and safe operational
persistence for greeny_ai collections.
Credentials and raw connection strings are NEVER printed or logged.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, Optional
from urllib.parse import urlsplit
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.database import Database
from pymongo.errors import PyMongoError

from app.config import settings

logger = logging.getLogger("greenie.database")

_mongo_client: Optional[MongoClient] = None


def get_greeny_ai_client() -> Optional[MongoClient]:
    """Return a cached MongoClient for the greeny_ai database if configured."""
    global _mongo_client
    if _mongo_client is not None:
        return _mongo_client

    if not settings.greeny_ai_mongo_url:
        return None

    url_val = settings.greeny_ai_mongo_url.get_secret_value()
    if not url_val.strip():
        return None

    try:
        _mongo_client = MongoClient(
            url_val,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=4000,
            socketTimeoutMS=10000,
            appname="GreenyAIKiosk",
        )
        return _mongo_client
    except Exception as exc:
        logger.error("Failed to initialize greeny_ai MongoClient: %s", type(exc).__name__)
        return None


def get_greeny_ai_db() -> Optional[Database]:
    """Get the logical greeny_ai database instance."""
    client = get_greeny_ai_client()
    if client is None:
        return None
    return client[settings.greeny_ai_mongo_database]


def check_mongo_health(db_override: Optional[Database] = None) -> Dict[str, Any]:
    """Safe health check returning connectivity status without credentials."""
    target_db = db_override if db_override is not None else get_greeny_ai_db()
    if target_db is None:
        return {
            "status": "disabled",
            "connected": False,
            "database": settings.greeny_ai_mongo_database,
            "message": "GREENY_AI_MONGO_URL is not configured. In-memory mode active.",
        }

    try:
        ping_res = target_db.client.admin.command("ping")
        is_ok = bool(ping_res.get("ok", 0) == 1)
        return {
            "status": "healthy" if is_ok else "degraded",
            "connected": is_ok,
            "database": target_db.name,
            "server_version": ping_res.get("version", "unknown"),
        }
    except PyMongoError as exc:
        logger.warning("MongoDB health check ping failed: %s", type(exc).__name__)
        return {
            "status": "unhealthy",
            "connected": False,
            "database": target_db.name,
            "error_category": type(exc).__name__,
        }
    except Exception as exc:
        logger.error("Unexpected error during MongoDB health check: %s", type(exc).__name__)
        return {
            "status": "unhealthy",
            "connected": False,
            "database": target_db.name,
            "error_category": type(exc).__name__,
        }


def create_indexes_for_greeny_ai(db: Database) -> Dict[str, list]:
    """Create indexes and TTL policies for all 7 greeny_ai operational collections.

    TTL: kiosk_sessions and messages expire automatically after retention period.
    """
    retention_seconds = int(settings.greeny_ai_retention_days * 86400)
    created: Dict[str, list] = {}

    # 1. kiosk_sessions
    db.kiosk_sessions.create_index([("session_id", ASCENDING)], unique=True)
    db.kiosk_sessions.create_index([("kiosk_id", ASCENDING), ("session_id", ASCENDING)])
    db.kiosk_sessions.create_index([("created_at", ASCENDING)], expireAfterSeconds=retention_seconds)
    created["kiosk_sessions"] = ["session_id_1", "kiosk_id_1_session_id_1", "created_at_ttl"]

    # 2. messages
    db.messages.create_index([("session_id", ASCENDING), ("created_at", ASCENDING)])
    db.messages.create_index([("created_at", ASCENDING)], expireAfterSeconds=retention_seconds)
    created["messages"] = ["session_id_1_created_at_1", "created_at_ttl"]

    # 3. customer_consents
    db.customer_consents.create_index([("session_id", ASCENDING), ("consent_type", ASCENDING)], unique=True)
    db.customer_consents.create_index([("timestamp", DESCENDING)])
    created["customer_consents"] = ["session_id_1_consent_type_1", "timestamp_-1"]

    # 4. sales_leads
    db.sales_leads.create_index([("lead_id", ASCENDING)], unique=True)
    db.sales_leads.create_index([("lead_type", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)])
    created["sales_leads"] = ["lead_id_1", "lead_type_1_status_1_created_at_-1"]

    # 5. quote_drafts
    db.quote_drafts.create_index([("quote_id", ASCENDING)], unique=True)
    db.quote_drafts.create_index([("session_id", ASCENDING), ("created_at", DESCENDING)])
    created["quote_drafts"] = ["quote_id_1", "session_id_1_created_at_-1"]

    # 6. unanswered_questions
    db.unanswered_questions.create_index([("timestamp", DESCENDING)])
    db.unanswered_questions.create_index([("category", ASCENDING)])
    created["unanswered_questions"] = ["timestamp_-1", "category_1"]

    # 7. approved_knowledge
    db.approved_knowledge.create_index([("doc_id", ASCENDING)], unique=True)
    db.approved_knowledge.create_index([("content_sha256", ASCENDING)])
    created["approved_knowledge"] = ["doc_id_1", "content_sha256_1"]

    for collection, field in (
        (db.customer_consents, 'timestamp'), (db.sales_leads, 'created_at'),
        (db.quote_drafts, 'created_at'), (db.unanswered_questions, 'timestamp'),
        (db['consent_operations'], 'created_at'),
    ):
        collection.create_index([(field, ASCENDING)], expireAfterSeconds=retention_seconds)

    # 8. backend_tickets (TTL on expires_at, unique on _id)
    # NOTE: MongoDB TTL monitor background thread runs asynchronously (~every 60s).
    # Uniqueness checks on _id and timestamp verification in application logic
    # remain strictly authoritative until background expiration cleanup.
    db.backend_tickets.create_index([("_id", ASCENDING)], unique=True)
    db.backend_tickets.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
    created["backend_tickets"] = ["_id_1", "expires_at_0"]

    # 9. backend_rates (TTL on expires_at)
    db.backend_rates.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
    created["backend_rates"] = ["expires_at_0"]

    # 10. backend_mutexes (unique on _id)
    db.backend_mutexes.create_index([("_id", ASCENDING)], unique=True)
    created["backend_mutexes"] = ["_id_1"]

    return created
