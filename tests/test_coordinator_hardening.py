"""Rigorous automated tests for hardened Greeny AI Coordinator.

Covers:
1. Mutual exclusion across independent coordinator instances sharing a test DB.
2. Cross-instance replay prevention under race conditions.
3. Atomic rate limit counting and window roll with find_one_and_update ReturnDocument.AFTER.
4. Simultaneous concurrent lock, ticket, and rate-limit attempts.
5. Worker crash and safe administrative lock recovery.
6. Leased lock expiry with monotonic fencing-token advancement.
7. Stale worker write prevention via fencing token verification.
8. Database outage fail-closed behavior in production mode.
9. Production configuration validation for all coordinator dependencies.
10. Authentication ticket validation before consume_ticket (audience, identity, expiry).
11. Coordinator metrics tracking for busy sessions, stale locks, rejected tickets, and rate limits.
12. GET /coordinator/metrics operational endpoint.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import time
import pytest
from pydantic import SecretStr
from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError, AutoReconnect

from app.config import settings
from app.coordination import (
    Coordinator,
    SessionBusyError,
    DatabaseConnectionError,
    CoordinationConfigurationError,
    get_coordinator,
    validate_coordinator_production_config,
)
from app.mongo_checkpointer import MongoCheckpointSaver
from app.server import api, generate_kiosk_ticket, verify_kiosk_token_internal
from app.sessions import session_manager
from test_mongo_persistence import MockDatabase


# ==============================================================================
# 1. Independent Coordinator Instances & Mutual Exclusion
# ==============================================================================

def test_two_independent_coordinators_mutual_exclusion():
    """Two independent coordinator instances sharing a database enforce strict mutual exclusion."""
    db = MockDatabase("test_coordination_db")
    coord1 = Coordinator(db)
    coord2 = Coordinator(db)

    session_id = "turn:sess-shared-01"

    # Coord 1 acquires the lock
    token1 = coord1.acquire(session_id)
    assert token1 is not None

    # Coord 2 attempts to acquire the same lock -> SessionBusyError
    with pytest.raises(SessionBusyError):
        coord2.acquire(session_id)

    assert coord2.get_metrics()["busy_sessions"] == 1

    # Coord 1 releases the lock
    assert coord1.release(session_id, token1) is True

    # Coord 2 can now acquire the lock
    token2 = coord2.acquire(session_id)
    assert token2 is not None
    assert token2 != token1

    coord2.release(session_id, token2)


def test_two_independent_coordinators_ticket_replay():
    """Tickets consumed by one coordinator are rejected by another sharing the database."""
    db = MockDatabase("test_coordination_db")
    coord1 = Coordinator(db)
    coord2 = Coordinator(db)

    digest = "sha256-shared-ticket-digest"

    # Coord 1 consumes ticket -> True
    assert coord1.consume_ticket(digest, ttl=60) is True

    # Coord 2 attempts to consume the same ticket -> False (replay prevented)
    assert coord2.consume_ticket(digest, ttl=60) is False
    assert coord2.get_metrics()["rejected_tickets"] == 1


def test_two_independent_coordinators_atomic_rate_limits():
    """Rate limit increments are atomic and shared across coordinator instances."""
    db = MockDatabase("test_coordination_db")
    coord1 = Coordinator(db)
    coord2 = Coordinator(db)

    client_key = "kiosk-gateway-rate-test"
    limit = 3

    assert coord1.allow(client_key, limit=limit, window=60) is True   # Count 1
    assert coord2.allow(client_key, limit=limit, window=60) is True   # Count 2
    assert coord1.allow(client_key, limit=limit, window=60) is True   # Count 3
    assert coord2.allow(client_key, limit=limit, window=60) is False  # Count 4 -> Rejected!

    assert coord2.get_metrics()["rate_limited_requests"] == 1


# ==============================================================================
# 2. Concurrency Tests (Simultaneous Attempts)
# ==============================================================================

def test_simultaneous_concurrent_lock_attempts():
    """When 10 threads simultaneously compete for one lock, exactly 1 succeeds."""
    db = MockDatabase("concurrent_test_db")
    coord = Coordinator(db)
    lock_key = "turn:race-session-999"

    results = []

    def attempt_acquire():
        try:
            tok = coord.acquire(lock_key)
            results.append(("success", tok))
        except SessionBusyError:
            results.append(("busy", None))

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(attempt_acquire) for _ in range(10)]
        for f in futures:
            f.result()

    successes = [r for r in results if r[0] == "success"]
    busies = [r for r in results if r[0] == "busy"]

    assert len(successes) == 1
    assert len(busies) == 9
    assert coord.get_metrics()["busy_sessions"] == 9

    # Clean up
    coord.release(lock_key, successes[0][1])


def test_simultaneous_concurrent_ticket_consumption():
    """When 10 threads race to consume the same ticket digest, exactly 1 succeeds."""
    db = MockDatabase("concurrent_test_db")
    coord = Coordinator(db)
    digest = "race-ticket-digest"

    def attempt_consume():
        return coord.consume_ticket(digest, ttl=60)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(attempt_consume) for _ in range(10)]
        consumed = [f.result() for f in futures]

    assert consumed.count(True) == 1
    assert consumed.count(False) == 9
    assert coord.get_metrics()["rejected_tickets"] == 9


def test_simultaneous_concurrent_rate_limiting():
    """When 15 threads call allow() with limit=5, exactly 5 succeed and 10 fail."""
    db = MockDatabase("concurrent_test_db")
    coord = Coordinator(db)
    key = "concurrent-user"
    limit = 5

    def attempt_allow():
        return coord.allow(key, limit=limit, window=60)

    with ThreadPoolExecutor(max_workers=15) as pool:
        futures = [pool.submit(attempt_allow) for _ in range(15)]
        allowed = [f.result() for f in futures]

    assert allowed.count(True) == 5
    assert allowed.count(False) == 10
    assert coord.get_metrics()["rate_limited_requests"] == 10


# ==============================================================================
# 3. Crash Recovery, Leased Locks & Fencing Tokens
# ==============================================================================

def test_worker_crash_and_administrative_lock_recovery():
    """An abandoned lock from a crashed worker can be administratively broken with fence increment."""
    db = MockDatabase("recovery_test_db")
    worker1 = Coordinator(db)
    worker2 = Coordinator(db)

    key = "turn:crashed-session"

    # Worker 1 acquires lock and then crashes
    tok1 = worker1.acquire(key)
    fence1 = worker1.get_fence(key)
    assert fence1 is not None

    # Worker 2 cannot acquire while held
    with pytest.raises(SessionBusyError):
        worker2.acquire(key)

    # Operator breaks abandoned lock
    assert worker2.break_lock(key, reason="Worker 1 process terminated") is True
    assert worker2.get_metrics()["stale_locks"] == 1

    # Worker 2 can now acquire, receiving an advanced fencing token
    tok2 = worker2.acquire(key)
    fence2 = worker2.get_fence(key)
    assert fence2 > fence1

    # Worker 1 attempts to release with old token -> cannot affect Worker 2's lock
    worker1.release(key, tok1)
    assert worker2.verify_fence(key, expected_fence=fence2) is True


def test_stale_worker_write_fenced_out():
    """A stale worker whose lock was recovered cannot verify its outdated fence."""
    db = MockDatabase("fence_test_db")
    worker1 = Coordinator(db)
    worker2 = Coordinator(db)

    key = "turn:fenced-session"

    tok1 = worker1.acquire(key)
    initial_fence = worker1.get_fence(key)

    # Administrative recovery advances fence
    worker2.break_lock(key, reason="recovery")
    tok2 = worker2.acquire(key)
    new_fence = worker2.get_fence(key)

    assert new_fence > initial_fence

    # Worker 1 attempts to write state checking its initial fence -> Rejected!
    assert worker1.verify_fence(key, expected_fence=initial_fence) is False

    # Worker 2's fence is valid
    assert worker2.verify_fence(key, expected_fence=new_fence) is True


def test_clear_abandoned_locks():
    """clear_abandoned_locks clears locks older than max_age_seconds."""
    db = MockDatabase("clear_locks_db")
    coord = Coordinator(db)

    # Insert an old lock directly
    old_time = datetime.now(timezone.utc) - timedelta(seconds=400)
    db["backend_mutexes"].insert_one({
        "_id": "turn:stale-1",
        "token": "tok-stale",
        "fence": 1,
        "created_at": old_time,
    })
    db["backend_mutexes"].insert_one({
        "_id": "turn:fresh-1",
        "token": "tok-fresh",
        "fence": 1,
        "created_at": datetime.now(timezone.utc),
    })

    cleared = coord.clear_abandoned_locks(older_than_seconds=300)
    assert cleared == 1

    # Fresh lock remains
    assert db["backend_mutexes"].find_one({"_id": "turn:fresh-1"}) is not None
    assert db["backend_mutexes"].find_one({"_id": "turn:stale-1"}) is None


# ==============================================================================
# 4. Fail-Closed Behavior on Database Outage
# ==============================================================================

class BrokenCollection:
    def __init__(self, name):
        self.name = name

    def insert_one(self, *a, **kw):
        raise ServerSelectionTimeoutError("MongoDB cluster unreachable")

    def find_one_and_update(self, *a, **kw):
        raise AutoReconnect("MongoDB connection dropped")

    def delete_many(self, *a, **kw):
        raise ServerSelectionTimeoutError("MongoDB cluster unreachable")

    def find(self, *a, **kw):
        raise ServerSelectionTimeoutError("MongoDB cluster unreachable")

    def find_one(self, *a, **kw):
        raise ServerSelectionTimeoutError("MongoDB cluster unreachable")


class BrokenDatabase:
    def __init__(self):
        self.name = "broken_db"

    def __getitem__(self, name):
        return BrokenCollection(name)

    def command(self, cmd):
        raise ServerSelectionTimeoutError("MongoDB ping failed")


def test_database_outage_fails_closed():
    """During a database outage, Coordinator fails closed (raises DatabaseConnectionError or rejects)."""
    broken_db = BrokenDatabase()
    coord = Coordinator(broken_db, fail_closed=True)

    # acquire fails closed -> DatabaseConnectionError
    with pytest.raises(DatabaseConnectionError):
        coord.acquire("session-outage")

    # consume_ticket fails closed -> DatabaseConnectionError
    with pytest.raises(DatabaseConnectionError):
        coord.consume_ticket("ticket-outage", 60)

    # allow fails closed -> DatabaseConnectionError
    with pytest.raises(DatabaseConnectionError):
        coord.allow("user-outage", limit=10)


# ==============================================================================
# 5. Production Configuration Validation
# ==============================================================================

def test_production_fail_closed_if_local_coordinator(monkeypatch):
    """In production, get_coordinator() raises CoordinationConfigurationError if local fallback is detected."""
    monkeypatch.setattr(settings, "environment", "production")

    # Checkpointer has no shared MongoDB coordinator
    monkeypatch.setattr(session_manager, "checkpointer", None)

    with pytest.raises(CoordinationConfigurationError) as exc_info:
        get_coordinator()
    assert "Production requires shared MongoDB coordination" in str(exc_info.value)


def test_validate_coordinator_production_config(monkeypatch):
    """validate_coordinator_production_config enforces all production dependencies."""
    monkeypatch.setattr(settings, "environment", "production")

    # 1. Missing MongoDB checkpointer
    monkeypatch.setattr(settings, "checkpointer_backend", "memory")
    with pytest.raises(CoordinationConfigurationError):
        validate_coordinator_production_config()

    monkeypatch.setattr(settings, "checkpointer_backend", "mongodb")

    # 2. Missing Mongo URL
    monkeypatch.setattr(settings, "greeny_ai_mongo_url", None)
    with pytest.raises(CoordinationConfigurationError):
        validate_coordinator_production_config()

    monkeypatch.setattr(settings, "greeny_ai_mongo_url", SecretStr("mongodb://localhost:27017"))

    # 3. Missing Auth Secret
    monkeypatch.setattr(settings, "kiosk_auth_secret", None)
    with pytest.raises(CoordinationConfigurationError):
        validate_coordinator_production_config()

    monkeypatch.setattr(settings, "kiosk_auth_secret", SecretStr("secret-123"))

    # 4. Missing Gateway Key
    monkeypatch.setattr(settings, "kiosk_gateway_key", None)
    with pytest.raises(CoordinationConfigurationError):
        validate_coordinator_production_config()

    monkeypatch.setattr(settings, "kiosk_gateway_key", SecretStr("gateway-456"))

    # 5. Wildcard allowed kiosk IDs
    monkeypatch.setattr(settings, "allowed_kiosk_ids", "*")
    with pytest.raises(CoordinationConfigurationError):
        validate_coordinator_production_config()

    monkeypatch.setattr(settings, "allowed_kiosk_ids", "kiosk-01,kiosk-02")

    # 6. Checkpointer is not MongoCheckpointSaver
    monkeypatch.setattr(session_manager, "checkpointer", None)
    with pytest.raises(CoordinationConfigurationError):
        validate_coordinator_production_config()

    # 7. Valid mock checkpointer with responding DB
    mock_db = MockDatabase("greeny_ai")
    saver = MongoCheckpointSaver(mock_db)
    monkeypatch.setattr(session_manager, "checkpointer", saver)

    # Valid configuration passes without error
    validate_coordinator_production_config()


# ==============================================================================
# 6. Ticket Validation Order (Before consume_ticket)
# ==============================================================================

def test_ticket_validation_order_and_audience(monkeypatch):
    """Validation (signature, timestamp, identity, audience) must fail before consume_ticket is called."""
    secret = "test-validation-secret"
    monkeypatch.setattr(settings, "allowed_kiosk_ids", "kiosk-authorized-01")

    # 1. Invalid signature: rejected without consuming
    bad_sig_ticket = f"v1.kiosk-authorized-01.{int(time.time())}.nonce123.invalidsig"
    with pytest.raises(Exception) as exc:
        verify_kiosk_token_internal(bad_sig_ticket, secret=secret, consume=True)
    assert "Invalid kiosk ticket signature" in str(exc.value)

    # 2. Expired ticket: rejected without consuming
    past_ts = int(time.time()) - 500
    exp_nonce = "abc123"
    exp_payload = f"kiosk-authorized-01:{past_ts}:{exp_nonce}"
    exp_sig = __import__("hmac").new(secret.encode(), exp_payload.encode(), __import__("hashlib").sha256).hexdigest()
    expired_ticket = f"v1.kiosk-authorized-01.{past_ts}.{exp_nonce}.{exp_sig}"
    with pytest.raises(Exception) as exc:
        verify_kiosk_token_internal(expired_ticket, secret=secret, ttl_seconds=300, consume=True)
    assert "expired" in str(exc.value).lower()

    # 3. Unauthorized audience kiosk: rejected without consuming
    unauthorized_ticket = generate_kiosk_ticket("kiosk-rogue-99", secret, ttl_seconds=300)
    with pytest.raises(Exception) as exc:
        verify_kiosk_token_internal(unauthorized_ticket, secret=secret, consume=True)
    assert "Forbidden" in str(exc.value)

    # 4. Identity mismatch: rejected without consuming
    valid_ticket = generate_kiosk_ticket("kiosk-authorized-01", secret, ttl_seconds=300)
    with pytest.raises(Exception) as exc:
        verify_kiosk_token_internal(valid_ticket, claimed_kiosk_id="kiosk-other", secret=secret, consume=True)
    assert "identity mismatch" in str(exc.value).lower()

    # 5. Legitimate ticket succeeds
    verified = verify_kiosk_token_internal(
        valid_ticket,
        claimed_kiosk_id="kiosk-authorized-01",
        secret=secret,
        consume=True,
    )
    assert verified == "kiosk-authorized-01"


# ==============================================================================
# 7. Metrics Endpoint & Health Check
# ==============================================================================

def test_coordinator_metrics_endpoint_and_health(monkeypatch):
    """Coordinator metrics are exposed via GET /coordinator/metrics and GET /health."""
    monkeypatch.setattr(settings, "kiosk_gateway_key", SecretStr("test-gateway-key"))
    client = TestClient(api)

    # 1. Health check includes coordinator metrics
    resp_health = client.get("/health")
    assert resp_health.status_code == 200
    data_health = resp_health.json()
    assert "coordinator" in data_health
    assert "busy_sessions" in data_health["coordinator"]
    assert "stale_locks" in data_health["coordinator"]
    assert "rejected_tickets" in data_health["coordinator"]
    assert "rate_limited_requests" in data_health["coordinator"]

    # 2. Unauthorized metrics request -> 401
    resp_unauth = client.get("/coordinator/metrics")
    assert resp_unauth.status_code == 401

    # 3. Authorized metrics request via X-Gateway-Key -> 200
    resp_metrics = client.get("/coordinator/metrics", headers={"X-Gateway-Key": "test-gateway-key"})
    assert resp_metrics.status_code == 200
    data_metrics = resp_metrics.json()
    assert "metrics" in data_metrics
    assert "active_locks" in data_metrics


def test_leased_lock_automatic_fenced_recovery():
    """An expired leased lock is automatically recovered with a monotonically incremented fence."""
    db = MockDatabase("lease_test_db")
    coord1 = Coordinator(db)
    coord2 = Coordinator(db)
    key = "turn:leased-turn"

    # Worker 1 acquires with a 1-second lease
    tok1 = coord1.acquire(key, lease_seconds=1)
    fence1 = coord1.get_fence(key)

    # Immediate second acquisition is rejected as busy
    with pytest.raises(SessionBusyError):
        coord2.acquire(key, lease_seconds=1)

    # Simulate passage of time past the 1-second lease
    past_time = datetime.now(timezone.utc) - timedelta(seconds=2)
    db["backend_mutexes"].find_one_and_update(
        {"_id": key},
        {"$set": {"expires_at": past_time}},
    )

    # Worker 2 now acquires: safely recovers the expired leased lock, advancing fence
    tok2 = coord2.acquire(key, lease_seconds=5)
    fence2 = coord2.get_fence(key)

    assert fence2 > fence1
    assert coord2.verify_fence(key, expected_fence=fence2) is True
    assert coord1.verify_fence(key, expected_fence=fence1) is False
    assert coord2.get_metrics()["stale_locks"] >= 1


def test_rate_limiting_bucket_roll():
    """Rate limit resets appropriately across time bucket rolls."""
    db = MockDatabase("bucket_test_db")
    coord = Coordinator(db)
    key = "bucket-user"

    # In window of 1 second:
    assert coord.allow(key, limit=2, window=1) is True
    assert coord.allow(key, limit=2, window=1) is True
    assert coord.allow(key, limit=2, window=1) is False

    # Sleep 1.1 seconds so bucket rolls over
    time.sleep(1.1)

    # New bucket: calls allowed again
    assert coord.allow(key, limit=2, window=1) is True
    assert coord.allow(key, limit=2, window=1) is True
    assert coord.allow(key, limit=2, window=1) is False
