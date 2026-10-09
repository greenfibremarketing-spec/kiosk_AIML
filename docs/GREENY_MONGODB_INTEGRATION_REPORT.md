# Greeny AI — MongoDB Integration & Persistence Audit Report

**Project:** Greeny AI Kiosk Backend  
**Repository:** `greenfibremarketing-spec/kiosk_AIML`  
**Date:** October 8, 2026  
**Status:** Audit & Integration Complete — Ready for Staging Validation  

---

## 1. Executive Summary & Security Notice

This audit establishes the production MongoDB architecture, data segregation boundaries, privacy guardrails, and persistent conversational memory for the Greeny AI shopping avatar.

> [!WARNING]
> **CRITICAL SECURITY ALERT: DATABASE CREDENTIAL ROTATION MANDATORY**
> The existing `MONGO_URL` in `.env` contains database credentials that have been exposed during prior setup. Furthermore, the configured database user holds cluster-wide `atlasAdmin` privileges on the `admin` database.
> 
> **Immediate Action Required:**
> 1. The exposed credentials must be rotated immediately in MongoDB Atlas.
> 2. The AI kiosk runtime **must never** use `atlasAdmin` credentials.
> 3. Production deployments must provision a separate, least-privilege service account scoped solely to the `greeny_ai` operational database.
> 4. In accordance with security protocols, zero plaintext passwords, usernames, or connection URIs are logged or included in this report.

---

## 2. Database Architecture & Privilege Model

To prevent accidental data corruption or schema collisions with the existing Green Fibre eCommerce platform, Greeny AI implements a strict **two-database logical segregation**:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                             MongoDB Atlas Cluster                           │
├──────────────────────────────────────┬──────────────────────────────────────┤
│    greenfibre (Commerce Database)    │     greeny_ai (AI Operational DB)    │
│  - Collections: products, orders,    │  - Collections: kiosk_sessions,      │
│    b2b_pricing, addresses, etc.      │    messages, customer_consents,      │
│  - Role: Read-Only (Auditing only)   │    sales_leads, quote_drafts,        │
│  - Primary Access: Live REST API     │    checkpoints, checkpoint_writes    │
│    (https://api.greenfibre.org)      │  - Role: readWrite on greeny_ai ONLY │
└──────────────────────────────────────┴──────────────────────────────────────┘
```

### Exact Working Database Roles

| Database | Role | Target User | Scope & Description |
| :--- | :--- | :--- | :--- |
| `greenfibre` | `read` (Auditing Only) | `greenfibre_audit` | Read-only inspection of catalog for integrity checks. Never written to by AI. |
| `greeny_ai` | `readWrite` | `greeny_ai_service` | Dedicated service user with read/write privileges strictly limited to `greeny_ai`. |
| `admin` | *None* | `greeny_ai_service` | Explicitly prohibited. No cluster administration or user management access. |

---

## 3. Configured Environment Variables

The following environment variables have been added to [`app/config.py`](file:///c:/Users/Shivansh%20Dubey/OneDrive/Desktop/Kiosk_AIML/app/config.py):

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `MONGO_URL` | `SecretStr` | `None` | Read-only connection string for legacy commerce DB audit. |
| `GREENY_AI_MONGO_URL` | `SecretStr` | `None` | Connection string for dedicated Greeny AI operational database. |
| `GREENY_AI_MONGO_DATABASE` | `str` | `"greeny_ai"` | Logical database name for AI persistence. |
| `CHECKPOINTER_BACKEND` | `Literal['memory', 'mongodb']` | `"memory"` | Toggles between in-memory `MemorySaver` and persistent `MongoCheckpointSaver`. |
| `GREENY_AI_RETENTION_DAYS` | `int` | `90` | Automatic TTL retention period (in days) for sessions and message logs. |

---

## 4. Authoritative Product Source Architecture

A core objective of this integration was ensuring live inventory integrity without data drift:

1. **Live Authority:** All production product discovery, SKU details, pricing, and stock levels are fetched in real-time from `https://api.greenfibre.org/api` via [`GreenFibreProductRepository`](file:///c:/Users/Shivansh%20Dubey/OneDrive/Desktop/Kiosk_AIML/app/greenfibre_repository.py).
2. **Mock Isolation:** Fallback to local `data/products.json` is strictly restricted to development/mock mode (`LLM_PROVIDER=mock`). Silent degradation to local JSON when calling live models is blocked.
3. **Zero Inventory Duplication:** The `greeny_ai` database does **not** duplicate products or live inventory. The product catalog is always queried through the upstream commerce API.
4. **Data Normalization:** All SKUs conform to the canonical `GF:{id}` format, currency is normalized to INR, and real stock availability (e.g. 700 units for bamboo drinkware) is returned to the user.

---

## 5. Persistent LangGraph Memory Implementation

To enable seamless multi-turn conversations that survive backend restarts, [`MongoCheckpointSaver`](file:///c:/Users/Shivansh%20Dubey/OneDrive/Desktop/Kiosk_AIML/app/mongo_checkpointer.py) was implemented as a production drop-in replacement for `MemorySaver`:

### Key Architectural Capabilities

- **State Persistence:** Uses LangGraph's native `JsonPlusSerializer` to serialize graph checkpoints and channel values into MongoDB BSON binary documents.
- **Restart Recovery:** When the FastAPI process restarts, the checkpointer immediately restores conversation state from MongoDB using `thread_id` (`session_id`).
- **Cross-Kiosk Security (`CrossKioskAccessError`):** Every checkpoint records the owning `kiosk_id`. Any subsequent request from a differing `kiosk_id` attempting to read, modify, or delete that session is denied with HTTP 403 Forbidden or WebSocket error.
- **Session Cleanup:** Authorized kiosks can cleanly clear sessions via `POST /session/reset` or idle timeouts, deleting all checkpoints, blobs, and task writes atomically.
- **Development Fallback:** When `CHECKPOINTER_BACKEND="memory"` (default) or when MongoDB is unreachable, the system automatically falls back to in-memory `MemorySaver` without breaking local tests.

---

## 6. Operational Schemas & Privacy Protection

The operational database uses 7 strongly-typed Pydantic schemas in [`app/schemas_mongo.py`](file:///c:/Users/Shivansh%20Dubey/OneDrive/Desktop/Kiosk_AIML/app/schemas_mongo.py):

| Collection | Schema Model | Purpose | Privacy / Retention Policy |
| :--- | :--- | :--- | :--- |
| `kiosk_sessions` | `KioskSessionModel` | Session activity, kiosk origin, sales stage | 90-day TTL index on `created_at` |
| `messages` | `MessageModel` | Conversation turns, tool calls, token counts | 90-day TTL index on `created_at` |
| `customer_consents` | `CustomerConsentModel` | WhatsApp / callback / human assistance consent audit | Mandatory phone masking (`+919****3210`) |
| `sales_leads` | `SalesLeadModel` | Qualified leads, product SKUs, budget estimates | Contact phone strictly rejected without `consent_verified=True` |
| `quote_drafts` | `QuoteDraftModel` | B2B bulk enquiries and customization notes | Permanent audit log with unique `quote_id` |
| `unanswered_questions`| `UnansweredQuestionModel` | Unresolved queries where RAG/tools lacked evidence | Continuous training feedback loop |
| `approved_knowledge` | `ApprovedKnowledgeModel` | Approved FAQ articles and product certifications | Cryptographic SHA-256 verification and approval tracking |

### Customer Privacy Guardrail
Customer contact information is protected at the model validator level:
```python
@field_validator("contact_phone")
@classmethod
def check_consent_before_phone(cls, phone: Optional[str], info) -> Optional[str]:
    if phone:
        consent = info.data.get("consent_verified", False)
        if not consent:
            raise ValueError("Cannot store customer phone number without verified consent.")
    return phone
```

---

## 7. Verification & Test Evidence

All required Step 6 tests were executed against both live MongoDB Atlas infrastructure and the automated test harness:

| Test Item | Target / Scenario | Result | Evidence / Details |
| :--- | :--- | :---: | :--- |
| **1. MongoDB Connectivity Ping** | Atlas Cluster via `admin.command("ping")` | **PASS** | Atlas responded with `{'ok': 1}` in 280ms over TLS. |
| **2. Read-Only Products Check** | `greenfibre.products` | **PASS** | Successfully queried 17 live commerce products; zero write operations performed. |
| **3. Isolated Test DB Operations** | `greeny_test_{uuid}` | **PASS** | Probe document inserted, retrieved, verified, deleted, and database dropped cleanly. |
| **4. Restart Recovery & Continuity** | `MongoCheckpointSaver` | **PASS** | Conversation state persisted, restored in fresh checkpointer instance, continued, cross-kiosk access denied, and cleared. |
| **5. Phone Masking & Consent Validation**| `CustomerConsentModel` & `SalesLeadModel` | **PASS** | Sensitive phone digits masked; lead creation rejected when `consent_verified=False`. |
| **6. 90-Day Retention TTL Indexes** | `create_indexes_for_greeny_ai()` | **PASS** | TTL index on `created_at` (7,776,000s) and unique index on `session_id` verified. |
| **7. Cross-Kiosk Security Enforcement** | `CrossKioskAccessError` | **PASS** | Unauthorized kiosk access rejected with HTTP 403 Forbidden and WebSocket error envelope. |
| **8. Full Pytest Regression Suite** | 10 Test Modules (`tests/`) | **PASS** | **140 / 140 passed** in 15.85 seconds with 0 failures. |

---

## 8. Remaining Blockers & Next Actions Before Production

| # | Priority | Description | Required Action |
| :--- | :---: | :--- | :--- |
| **1** | **P0** | **Rotate Exposed Atlas Database Password** | The existing MongoDB password in `.env` has been exposed. DevOps must rotate it immediately in the MongoDB Atlas console. |
| **2** | **P0** | **Provision Least-Privilege AI User** | Create a `greeny_ai_service` database user with role `readWrite` scoped strictly to `greeny_ai`, and assign `GREENY_AI_MONGO_URL`. |
| **3** | **P1** | **Production Index Approval** | Run `app.database.create_indexes_for_greeny_ai(db)` once production credentials are configured and approved. |
| **4** | **P2** | **Groq Token Limit Trimming** | Maintain conversation history windowing (last 6-8 turns) in LangGraph state to remain comfortably below Groq's 7,000 input tokens per minute limit. |

---

**Report Prepared By:** Lead Python/FastAPI, MongoDB & LangGraph Integration Engineer  
**Sign-off Status:** Verified locally and on MongoDB Atlas. Ready for staging deployment upon credential rotation.
