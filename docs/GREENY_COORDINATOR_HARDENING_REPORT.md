# Greeny AI Backend Coordinator Hardening Report

**Date:** 2026-10-09  
**Module:** `app/coordination.py`  
**Status:** COMPLETE & VERIFIED  
**Test Suite Result:** **230 PASSED**, **0 FAILED**, **1 SKIPPED** (opt-in external integration marker)

---

## 1. Executive Summary

`app/coordination.py` has been systematically hardened to guarantee distributed mutual exclusion, ticket single-use semantics, atomic rate limiting, and fail-closed safety across multiple FastAPI worker processes and Greeny kiosks.

All 12 requirements outlined in the hardening mission have been implemented and validated through 16 new automated unit/concurrency tests alongside the full 214 regression test suite.

---

## 2. Hardening Highlights & Architecture

### 2.1. Atomic Rate Limiting (Requirement 1)
- **Previous implementation:** Used `update_one` with `$inc` followed by a separate `find_one` read. This suffered from race conditions under high concurrency where interleaved updates could cause inconsistent rate enforcement.
- **Hardened implementation:** Implemented atomic `find_one_and_update` using `ReturnDocument.AFTER` and `upsert=True`. The returned count is guaranteed to be the exact post-increment count for that atomic transaction. On concurrent upsert collision, `DuplicateKeyError` is gracefully handled with a direct atomic increment.

### 2.2. Production Fail-Closed Safety (Requirement 2 & 3)
- In development/testing (`ENVIRONMENT != "production"`), the coordinator supports local process-memory fallback.
- In production (`ENVIRONMENT == "production"`):
  - `get_coordinator()` strictly verifies that `session_manager.checkpointer` exposes a shared `Coordinator` instance backed by an active MongoDB database. If local memory fallback is detected or the checkpointer lacks shared coordination, `CoordinationConfigurationError` is raised immediately.
  - `validate_coordinator_production_config()` enforces that `CHECKPOINTER_BACKEND == "mongodb"`, `GREENY_AI_MONGO_URL` is set, `KIOSK_AUTH_SECRET` and `KIOSK_GATEWAY_KEY` are configured, `ALLOWED_KIOSK_IDS` is an explicit allowlist (not `*`), the database is reachable via `ping`, and coordinator indexes are created.
  - During MongoDB outages (`ServerSelectionTimeoutError`, `AutoReconnect`, `PyMongoError`), `acquire()`, `consume_ticket()`, and `allow()` fail closed: locks cannot be acquired uncoordinatedly (raising `DatabaseConnectionError`), replay protection rejects unverified tickets, and rate limiting blocks unmetered requests.

### 2.3. Session Lock Lifecycle & Leased Mutexes with Fencing Tokens (Requirement 4 & 5)
- **Monotonic Fencing Tokens:** Every lock acquisition or recovery advances an atomic monotonic counter in `backend_fences`.
- **Stale Worker Protection:** If a worker crashes or pauses and its lock is administratively broken or expires, the fencing counter advances. When the stale worker resumes and attempts state mutation, `verify_fence(key, expected_fence)` rejects the write. Furthermore, the stale worker's old token cannot release the newly assigned lock.
- **Administrative Recovery Tooling:**
  - `coord.break_lock(key, reason)`: Administratively clears a lock while incrementing the fence counter.
  - `coord.clear_abandoned_locks(older_than_seconds)`: Safely sweeps stale locks older than the specified threshold.
  - `coord.list_active_locks()`: Exposes active lock keys, tokens, creation times, and fences.
- **Leased Locks:** Optional `lease_seconds` allows leased locks. An expired lease can be recovered by a waiting worker, but only by atomically advancing the fencing counter.
- **Lifecycle Audits:** WebSocket disconnect, HTTP turn exceptions, cancellations, and session resets all guarantee lock release through `finally` blocks, guarding against unassigned variables and double releases.

### 2.4. Authentication-Ticket Validation & Audience Enforcement (Requirement 6 & 7)
- In `verify_kiosk_token_internal`:
  1. Ticket format (`v1.<kiosk_id>.<ts>.<nonce>.<sig>`) is parsed.
  2. Timestamp and expiration (`(now - ts) <= ttl_seconds`) are verified.
  3. HMAC SHA-256 signature is verified.
  4. Audience restrictions (`settings.allowed_kiosk_ids`) are verified.
  5. Kiosk identity match (`claimed_kiosk_id == token_kiosk_id`) is verified.
  6. **Only after all checks pass** is `consume_ticket` invoked. Invalid/forged/expired tickets never touch the replay cache.
- **Single-Use Semantics:**
  - WebSocket upgrade (`GET /ws/{session_id}`): Consumes ticket immediately during handshake (`consume=True`). Reconnection attempts require minting a fresh ticket via `POST /auth/kiosk/token`.
  - In-session turns: Each customer turn includes a unique `turn_id` whose digest is consumed (`86400s` TTL), preventing replay over the connection.

### 2.5. MongoDB TTL Indexes (Requirement 8)
- Configured in `app/database.py` and `Coordinator.ensure_indexes()`:
  - `backend_tickets`: unique index on `_id`, TTL index on `expires_at` (`expireAfterSeconds=0`).
  - `backend_rates`: TTL index on `expires_at` (`expireAfterSeconds=0`).
  - `backend_mutexes`: unique index on `_id`.
  - `backend_fences`: unique index on `_id`.
- **Documented Semantics:** MongoDB TTL monitor thread executes asynchronously every ~60 seconds. Documents whose `expires_at` has passed remain in the collection until the cleanup thread runs. Uniqueness checks on `_id` and application timestamp validation remain authoritative during this window.

### 2.6. Operational Metrics & Monitoring (Requirement 11)
- The coordinator tracks four dedicated metrics:
  - `busy_sessions`: Contended lock attempts rejected with `SessionBusyError`.
  - `stale_locks`: Abandoned or expired locks broken/recovered.
  - `rejected_tickets`: Duplicate or replayed tickets rejected.
  - `rate_limited_requests`: Requests rejected by rate limiters.
- Metrics are exposed in `GET /health` and via the authenticated endpoint `GET /coordinator/metrics` (requires gateway credentials).

---

## 3. Test Verification Results

### Hardening Test Suite (`tests/test_coordinator_hardening.py`)
| Test Name | Description | Status |
|---|---|:---:|
| `test_two_independent_coordinators_mutual_exclusion` | Verifies lock exclusion between independent instances sharing a DB | **PASS** |
| `test_two_independent_coordinators_ticket_replay` | Cross-instance single-use ticket replay rejection | **PASS** |
| `test_two_independent_coordinators_atomic_rate_limits` | Atomic shared rate counter across instances | **PASS** |
| `test_simultaneous_concurrent_lock_attempts` | 10 concurrent threads competing for 1 lock; 1 winner, 9 busy | **PASS** |
| `test_simultaneous_concurrent_ticket_consumption` | 10 concurrent threads consuming same ticket; 1 success, 9 rejected | **PASS** |
| `test_simultaneous_concurrent_rate_limiting` | 15 concurrent threads with limit=5; 5 allowed, 10 rejected | **PASS** |
| `test_worker_crash_and_administrative_lock_recovery` | Operator break_lock recovery after worker crash | **PASS** |
| `test_stale_worker_write_fenced_out` | Superseded worker with stale fence is rejected from state write | **PASS** |
| `test_clear_abandoned_locks` | Batch clearing of abandoned locks older than cutoff | **PASS** |
| `test_database_outage_fails_closed` | DatabaseConnectionError raised on DB connection drops in production | **PASS** |
| `test_production_fail_closed_if_local_coordinator` | CoordinationConfigurationError on unshared checkpointer | **PASS** |
| `test_validate_coordinator_production_config` | Enforces URL, secrets, gateway key, allowlist, checkpointer, and ping | **PASS** |
| `test_ticket_validation_order_and_audience` | Verifies validation precedes consumption; tests audience restriction | **PASS** |
| `test_coordinator_metrics_endpoint_and_health` | Validates GET /coordinator/metrics and GET /health responses | **PASS** |
| `test_leased_lock_automatic_fenced_recovery` | Automatic recovery of expired leased lock with fence increment | **PASS** |
| `test_rate_limiting_bucket_roll` | Rate limit window roll over bucket boundary | **PASS** |

### Complete Pytest Suite Summary
```
================== 230 passed, 1 skipped, 1 warning in 3.13s ==================
```
- **Total Tests:** 231
- **Passed:** 230
- **Failed:** 0
- **Skipped:** 1 (`greenfibre_live_repo_schema` - opt-in marker `--run-live`)
- **Blocked:** 0
