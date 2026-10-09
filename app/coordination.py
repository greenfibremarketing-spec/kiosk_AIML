"""Cross-worker exclusion and replay protection; local memory only for development.

Mongo mutexes support leased locks with fencing-token verification and clear
administrative recovery tooling.

NOTE ON MONGODB TTL INDEXES:
MongoDB TTL monitor thread runs asynchronously (typically once every 60 seconds).
Documents whose `expires_at` has elapsed may remain in the database for up to
60 seconds before background deletion. Uniqueness constraints (_id) and timestamp
verifications in application logic remain strictly authoritative until expiration cleanup.
"""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from threading import RLock
from uuid import uuid4
import time
import logging
from typing import Optional, Dict, Any, List, Tuple
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

logger = logging.getLogger("greenie.coordination")


class SessionBusyError(RuntimeError):
    """Raised when a requested session or inference slot is already in use."""
    pass


class DatabaseConnectionError(Exception):
    """Raised when MongoDB is unreachable in production mode."""
    pass


class CoordinationConfigurationError(RuntimeError):
    """Raised when coordinator configuration is invalid or missing dependencies."""
    pass


class StaleLockError(RuntimeError):
    """Raised when an operation with an outdated fencing token attempts state mutation."""
    pass


class Coordinator:
    def __init__(self, db=None, fail_closed: Optional[bool] = None):
        self.db = db
        self._fail_closed = fail_closed
        self.lock = RLock()
        self.active: Dict[str, str] = {}
        self.fences: Dict[str, int] = {}
        self.leases: Dict[str, float] = {}
        self.rates: Dict[str, Tuple[int, int]] = {}
        self.tickets: Dict[str, float] = {}
        self.metrics: Dict[str, int] = {
            "busy_sessions": 0,
            "stale_locks": 0,
            "rejected_tickets": 0,
            "rate_limited_requests": 0,
        }

    @property
    def should_fail_closed(self) -> bool:
        if self._fail_closed is not None:
            return self._fail_closed
        try:
            from app.config import settings
            return settings.environment == "production"
        except Exception:
            return False

    def record_metric(self, name: str, count: int = 1) -> None:
        with self.lock:
            if name in self.metrics:
                self.metrics[name] += count
        if self.db is not None:
            try:
                self.db['backend_metrics'].update_one(
                    {'_id': name},
                    {'$inc': {'count': count}},
                    upsert=True,
                )
            except Exception:
                pass

    def get_metrics(self) -> Dict[str, int]:
        with self.lock:
            return dict(self.metrics)

    def reset_metrics(self) -> None:
        with self.lock:
            for k in self.metrics:
                self.metrics[k] = 0

    def ensure_indexes(self) -> None:
        """Ensure TTL and uniqueness indexes for coordinator collections.

        TTL indexes are configured with expireAfterSeconds=0 on 'expires_at'.
        Note: TTL deletion is asynchronous, but uniqueness checks on _id and
        timestamp validation in application code remain authoritative until cleanup.
        """
        if self.db is None:
            return
        try:
            self.db['backend_mutexes'].create_index([('_id', 1)], unique=True)
            self.db['backend_tickets'].create_index([('_id', 1)], unique=True)
            self.db['backend_tickets'].create_index([('expires_at', 1)], expireAfterSeconds=0)
            self.db['backend_rates'].create_index([('expires_at', 1)], expireAfterSeconds=0)
            self.db['backend_fences'].create_index([('_id', 1)], unique=True)
        except Exception as exc:
            logger.warning("Could not ensure coordinator indexes: %s", type(exc).__name__)

    def acquire(self, key: str, lease_seconds: Optional[int] = None) -> str:
        token = uuid4().hex
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=lease_seconds) if lease_seconds else None

        if self.db is not None:
            try:
                fence_doc = self.db['backend_fences'].find_one_and_update(
                    {'_id': key},
                    {'$inc': {'fence': 1}},
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
                current_fence = fence_doc['fence'] if fence_doc else 1

                self.db['backend_mutexes'].insert_one({
                    '_id': key,
                    'token': token,
                    'fence': current_fence,
                    'created_at': now,
                    'expires_at': expires_at,
                })
                return token
            except DuplicateKeyError:
                if lease_seconds is not None:
                    try:
                        existing = self.db['backend_mutexes'].find_one({'_id': key})
                        if existing and existing.get('expires_at') and existing['expires_at'] < now:
                            self.record_metric('stale_locks', 1)
                            fence_doc = self.db['backend_fences'].find_one_and_update(
                                {'_id': key},
                                {'$inc': {'fence': 1}},
                                upsert=True,
                                return_document=ReturnDocument.AFTER,
                            )
                            recovered_fence = fence_doc['fence'] if fence_doc else (existing.get('fence', 1) + 1)
                            recovered = self.db['backend_mutexes'].find_one_and_update(
                                {'_id': key, 'token': existing['token']},
                                {'$set': {
                                    'token': token,
                                    'fence': recovered_fence,
                                    'created_at': now,
                                    'expires_at': expires_at,
                                }},
                                return_document=ReturnDocument.AFTER,
                            )
                            if recovered:
                                logger.warning("Recovered expired leased lock on '%s' (fence=%d)", key, recovered_fence)
                                return token
                    except PyMongoError as pe:
                        logger.error("DB error checking expired lease on '%s': %s", key, type(pe).__name__)
                        if self.should_fail_closed:
                            raise DatabaseConnectionError(f"Database error during lock recovery: {pe}") from pe

                self.record_metric('busy_sessions', 1)
                raise SessionBusyError('Session is busy.') from None
            except PyMongoError as exc:
                logger.error("Database outage during acquire for key '%s': %s", key, type(exc).__name__)
                if self.should_fail_closed:
                    raise DatabaseConnectionError(f"Database error during lock acquisition: {exc}") from exc
                self.record_metric('busy_sessions', 1)
                raise SessionBusyError('Session is busy.') from exc
        else:
            with self.lock:
                now_ts = time.time()
                if key in self.active:
                    if lease_seconds is not None and key in self.leases and self.leases[key] < now_ts:
                        self.record_metric('stale_locks', 1)
                        self.fences[key] = self.fences.get(key, 0) + 1
                        self.active[key] = token
                        self.leases[key] = now_ts + lease_seconds
                        return token
                    self.record_metric('busy_sessions', 1)
                    raise SessionBusyError('Session is busy.')
                self.fences[key] = self.fences.get(key, 0) + 1
                self.active[key] = token
                if lease_seconds is not None:
                    self.leases[key] = now_ts + lease_seconds
            return token

    def release(self, key: str, token: str) -> bool:
        if self.db is not None:
            try:
                res = self.db['backend_mutexes'].delete_many({'_id': key, 'token': token})
                return res.deleted_count > 0
            except PyMongoError as exc:
                logger.error("Database outage during release for key '%s': %s", key, type(exc).__name__)
                if self.should_fail_closed:
                    raise DatabaseConnectionError(f"Database error during lock release: {exc}") from exc
                return False
        else:
            with self.lock:
                if self.active.get(key) == token:
                    self.active.pop(key, None)
                    self.leases.pop(key, None)
                    return True
                return False

    @contextmanager
    def exclusive(self, key: str, lease_seconds: Optional[int] = None):
        token = self.acquire(key, lease_seconds=lease_seconds)
        try:
            yield
        finally:
            self.release(key, token)

    def get_fence(self, key: str) -> Optional[int]:
        """Return the current fencing counter for a key."""
        if self.db is not None:
            try:
                doc = self.db['backend_fences'].find_one({'_id': key})
                return doc['fence'] if doc else None
            except PyMongoError as exc:
                if self.should_fail_closed:
                    raise DatabaseConnectionError(f"Database error getting fence: {exc}") from exc
                return None
        with self.lock:
            return self.fences.get(key)

    def verify_fence(self, key: str, expected_fence: int) -> bool:
        """Verify that expected_fence matches the active lock's fence.

        Prevents stale/superseded workers from writing state after an abandoned lock
        was cleared or re-acquired.
        """
        if self.db is not None:
            try:
                doc = self.db['backend_mutexes'].find_one({'_id': key})
                if doc and doc.get('fence') == expected_fence:
                    return True
                return False
            except PyMongoError as exc:
                if self.should_fail_closed:
                    raise DatabaseConnectionError(f"Database error verifying fence: {exc}") from exc
                return False
        with self.lock:
            return self.fences.get(key) == expected_fence and key in self.active

    def break_lock(self, key: str, reason: str = "administrative recovery") -> bool:
        """Administratively break an abandoned lock while monotonically incrementing the fencing token."""
        if self.db is not None:
            try:
                self.db['backend_fences'].find_one_and_update(
                    {'_id': key},
                    {'$inc': {'fence': 1}},
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
                res = self.db['backend_mutexes'].delete_many({'_id': key})
                if res.deleted_count > 0:
                    self.record_metric('stale_locks', res.deleted_count)
                    logger.warning("Administrative lock break for '%s': reason=%s", key, reason)
                    return True
                return False
            except PyMongoError as exc:
                logger.error("DB error breaking lock '%s': %s", key, type(exc).__name__)
                if self.should_fail_closed:
                    raise DatabaseConnectionError(f"Database error breaking lock: {exc}") from exc
                return False
        with self.lock:
            if key in self.active:
                self.fences[key] = self.fences.get(key, 0) + 1
                self.active.pop(key, None)
                self.leases.pop(key, None)
                self.record_metric('stale_locks', 1)
                logger.warning("Administrative local lock break for '%s': reason=%s", key, reason)
                return True
            return False

    def clear_abandoned_locks(self, older_than_seconds: int = 300) -> int:
        """Clear all active locks created older than older_than_seconds, fencing out old workers."""
        if self.db is not None:
            try:
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
                stale_docs = list(self.db['backend_mutexes'].find({'created_at': {'$lt': cutoff}}))
                cleared = 0
                for doc in stale_docs:
                    if self.break_lock(doc['_id'], reason=f"abandoned lock older than {older_than_seconds}s"):
                        cleared += 1
                return cleared
            except PyMongoError as exc:
                if self.should_fail_closed:
                    raise DatabaseConnectionError(f"Database error clearing abandoned locks: {exc}") from exc
                return 0
        with self.lock:
            cleared = 0
            now_ts = time.time()
            stale_keys = [k for k, exp in self.leases.items() if exp < now_ts]
            for k in stale_keys:
                if self.break_lock(k, reason="expired lease"):
                    cleared += 1
            return cleared

    def list_active_locks(self) -> List[Dict[str, Any]]:
        """List currently held mutexes and their fencing/creation metadata."""
        if self.db is not None:
            try:
                docs = list(self.db['backend_mutexes'].find({}))
                result = []
                for d in docs:
                    item = dict(d)
                    if 'created_at' in item and isinstance(item['created_at'], datetime):
                        item['created_at'] = item['created_at'].isoformat()
                    if 'expires_at' in item and isinstance(item['expires_at'], datetime):
                        item['expires_at'] = item['expires_at'].isoformat()
                    result.append(item)
                return result
            except PyMongoError as exc:
                if self.should_fail_closed:
                    raise DatabaseConnectionError(f"Database error listing locks: {exc}") from exc
                return []
        with self.lock:
            return [
                {
                    '_id': k,
                    'token': v,
                    'fence': self.fences.get(k, 1),
                    'lease_expires': self.leases.get(k),
                }
                for k, v in self.active.items()
            ]

    def consume_ticket(self, digest: str, ttl: int) -> bool:
        now = datetime.now(timezone.utc)
        if self.db is not None:
            try:
                self.db['backend_tickets'].insert_one({
                    '_id': digest,
                    'expires_at': now + timedelta(seconds=ttl + 30),
                })
                return True
            except DuplicateKeyError:
                self.record_metric('rejected_tickets', 1)
                return False
            except PyMongoError as exc:
                logger.error("Database outage during consume_ticket: %s", type(exc).__name__)
                self.record_metric('rejected_tickets', 1)
                if self.should_fail_closed:
                    raise DatabaseConnectionError(f"Database error during ticket consumption: {exc}") from exc
                return False
        with self.lock:
            now_ts = time.time()
            self.tickets = {k: v for k, v in self.tickets.items() if v > now_ts}
            if digest in self.tickets:
                self.record_metric('rejected_tickets', 1)
                return False
            self.tickets[digest] = now_ts + ttl + 30
            return True

    def allow(self, key: str, limit: int, window: int = 60) -> bool:
        bucket = int(time.time()) // window
        if self.db is not None:
            ident = f'{key}:{bucket}'
            col = self.db['backend_rates']
            try:
                doc = col.find_one_and_update(
                    {'_id': ident},
                    {
                        '$inc': {'count': 1},
                        '$setOnInsert': {
                            'expires_at': datetime.now(timezone.utc) + timedelta(seconds=window * 2),
                        },
                    },
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
            except DuplicateKeyError:
                doc = col.find_one_and_update(
                    {'_id': ident},
                    {'$inc': {'count': 1}},
                    return_document=ReturnDocument.AFTER,
                )
            except PyMongoError as exc:
                logger.error("Database outage during rate limit check for '%s': %s", key, type(exc).__name__)
                self.record_metric('rate_limited_requests', 1)
                if self.should_fail_closed:
                    raise DatabaseConnectionError(f"Database error during rate limit check: {exc}") from exc
                return False

            if not doc or 'count' not in doc:
                self.record_metric('rate_limited_requests', 1)
                return False

            allowed = doc['count'] <= limit
            if not allowed:
                self.record_metric('rate_limited_requests', 1)
            return allowed
        with self.lock:
            self.rates = {k: v for k, v in self.rates.items() if v[0] == bucket}
            count = self.rates.get(key, (bucket, 0))[1] + 1
            self.rates[key] = (bucket, count)
            allowed = count <= limit
            if not allowed:
                self.record_metric('rate_limited_requests', 1)
            return allowed


local_coordinator = Coordinator()


def get_coordinator() -> Coordinator:
    from app.config import settings
    from app.sessions import session_manager

    saver = session_manager.checkpointer
    coord = getattr(saver, 'coordinator', None)

    if settings.environment == 'production':
        if coord is None or getattr(coord, 'db', None) is None:
            raise CoordinationConfigurationError(
                "Production requires shared MongoDB coordination; local coordinator not allowed in production."
            )
        return coord

    return coord or local_coordinator


def validate_coordinator_production_config() -> None:
    """Validate all coordinator dependencies for production environments."""
    from app.config import settings
    from app.sessions import session_manager
    from app.mongo_checkpointer import MongoCheckpointSaver

    if settings.environment != 'production':
        return

    if settings.checkpointer_backend != 'mongodb':
        raise CoordinationConfigurationError(
            "Production configuration error: CHECKPOINTER_BACKEND must be 'mongodb'."
        )

    if not settings.greeny_ai_mongo_url or not settings.greeny_ai_mongo_url.get_secret_value().strip():
        raise CoordinationConfigurationError(
            "Production configuration error: GREENY_AI_MONGO_URL must be configured."
        )

    if not settings.kiosk_auth_secret or not settings.kiosk_auth_secret.get_secret_value().strip():
        raise CoordinationConfigurationError(
            "Production configuration error: KIOSK_AUTH_SECRET must be configured."
        )

    if not settings.kiosk_gateway_key or not settings.kiosk_gateway_key.get_secret_value().strip():
        raise CoordinationConfigurationError(
            "Production configuration error: KIOSK_GATEWAY_KEY must be configured."
        )

    if not settings.allowed_kiosk_ids or settings.allowed_kiosk_ids.strip() == '*':
        raise CoordinationConfigurationError(
            "Production configuration error: ALLOWED_KIOSK_IDS must be an explicit allowlist in production."
        )

    checkpointer = session_manager.checkpointer
    if not isinstance(checkpointer, MongoCheckpointSaver):
        raise CoordinationConfigurationError(
            "Production configuration error: Session checkpointer must be MongoCheckpointSaver."
        )

    coord = getattr(checkpointer, 'coordinator', None)
    if coord is None or getattr(coord, 'db', None) is None:
        raise CoordinationConfigurationError(
            "Production configuration error: Checkpointer must expose a shared MongoDB coordinator."
        )

    try:
        coord.db.command('ping')
    except Exception as exc:
        raise CoordinationConfigurationError(
            f"Production configuration error: MongoDB coordinator database unreachable: {exc}"
        ) from exc

    coord.ensure_indexes()
