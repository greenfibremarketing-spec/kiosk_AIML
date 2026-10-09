# Greeny AI Kiosk — Project Status Report

**Review date:** 9 October 2026
**Repository:** `greenfibremarketing-spec/kiosk_AIML`
**Reviewed branch:** `feature/greeny-reliability-fixes`
**Latest local commit:** `4163bf2` — Add Greeny architecture audit and validated sales state

## 1. Overall status

Greeny now has a substantial working Python backend: conversational AI, website
product integration, retrieval, structured sales actions, HTTP/WebSocket interfaces,
kiosk authentication and optional MongoDB persistence. It has progressed beyond the
original static-catalogue chatbot.

**Current assessment: integration/testing stage; not yet ready for an unconditional
production sign-off.** The latest test run has one failing certification-safety test.
Source review also identifies consent, inventory-display, interruption, persistence
and security gaps that passing tests do not establish as solved.

This report describes the current working tree, including uncommitted files. It does
not claim that all of this code is committed, pushed, deployed or integrated with a
running frontend. No application code or Git staging was changed for this review.

## 2. Work completed so far

| Area | Implemented work | Current qualification |
| --- | --- | --- |
| Backend foundation | FastAPI server, configuration, REST chat, catalogue, health and session-reset routes | Working implementation retained rather than rebuilt. |
| AI conversation | LangChain models for Groq, Anthropic and mock; LangGraph tool loop, checkpointed messages, spoken-text cleanup, retry and recursion handling | Available; live model quality/latency was not retested in this review. |
| Knowledge retrieval | Markdown/text ingestion, Hugging Face embeddings, local FAISS index, similarity filtering and document-source metadata | Implemented. Document approval and SKU-level evidence are still separate requirements. |
| Initial product ingestion | MongoDB product chunking, local JSONL export, optional MongoDB chunk writes, stable chunk identifiers and metadata | Implemented independently of operational persistence. Ingestion was not run in this review. |
| Product repository | Read-only interface with JSON development adapter and Green Fibre HTTP adapter | Website API is the configured default source; JSON is restricted to mock use. |
| Website integration | Pydantic response validation, pagination, timeouts, error handling, rate-limit cooldown, descriptive cache, fresh detail requests | Product list/detail routes integrated. The suite contains a real public catalogue-read test. |
| Product identity | Canonical `GF:<product_id>` and `GF:<product_id>:<variant_id>` identifiers, variant matching, images and product URLs | Implemented without inventing merchant SKUs. |
| Product tools | Search by keyword/category/budget/occasion, exact details, stock check, gift-set discovery | Unknown stock and missing prices handled safely in repository tools; UI action generation still has gaps. |
| Business handoffs | Unsubmitted corporate enquiry drafts and website product-page handoffs | No real order/payment/CRM submission workflow demonstrated. Product-page handoff is not a checkout API integration. |
| Structured sales state | Pydantic state with stages, intent, budget, quantity, preferences, selected SKUs, consent/lead/quote statuses | Now connected to the turn result and checkpoint update path; no longer merely a standalone model. |
| Sales engine | Deterministic qualification, product/category display, price bands, customization, contact-collection and human-assistance actions | Functional initial rules; not a complete or fully validated sales policy engine. |
| Kiosk security | Trusted-gateway ticket issuance, signed kiosk tickets, expiry checks, identity validation, ownership checks and production configuration guards | Implemented, with important shared-secret/replay/concurrency limitations below. |
| Persistence | Custom MongoDB LangGraph saver plus operational session/message/consent/lead/knowledge helpers and index definitions | Implemented locally; deployment safety and complete end-to-end wiring are not established. |
| Frontend protocol | Legacy HTTP/WS support, `/v1` aliases, optional structured actions/stages, WS v2 token/sentence/action/done events | Backend contract exists; actual Electron/React frontend is not present in this repository. |
| Reliability | Generic errors on many paths, message bounds, full UUID session IDs, reset-error propagation, HTTP request IDs | Improved; not equivalent to complete abuse protection or privacy coverage. |
| Tests and CI | Product, API, state, sales, authentication, persistence and protocol tests; GitHub Actions test/dependency checks | Latest local result: 181 passed, 1 failed. Remote CI status not checked. |
| Documentation | Architecture, reliability, deployment, frontend handoff, MongoDB and acceptance reports; OpenAPI artifact | Extensive, but some claims are outdated or stronger than the current evidence supports. |

## 3. Current data flow

```text
Kiosk / frontend
  -> FastAPI chat or WebSocket
  -> Kiosk identity / session ownership checks
  -> LangGraph conversation
       -> LLM and product tools
            -> Green Fibre product API (authoritative commerce source)
            -> JSON only for mock development
       -> FAISS knowledge retrieval
  -> Response cleanup / factual checks
  -> Deterministic sales transition and structured UI action
  -> Sales-state checkpoint update
  -> Optional operational conversation storage
  -> Spoken reply + stage/action metadata to frontend
```

MongoDB has two distinct roles: the earlier product-chunk ingestion workflow, and
the new operational/checkpoint storage. Product chunks are not automatically a live
stock source. FAISS remains the local knowledge-retrieval system.

The sales engine can read the cached catalogue separately from live product tools.
Consequently, live verification in a tool does not by itself prove that every UI
card contains freshly verified stock or price.

## 4. Current API and integration coverage

Present in `app/server.py`:

- `POST /chat` and `/v1/chat`: reply, session ID, optional sales stage and UI action.
- `POST /session/reset` and `/v1/session/reset`: reset with kiosk ownership checks.
- `GET /products`: repository catalogue for frontend display.
- `GET /health`: application configuration and operational database health.
- `POST /auth/kiosk/token`: ticket issuance for a trusted gateway.
- `WS /ws/{session_id}`: legacy and v2 events, keepalive and cancel-message handling.
- `GET /ws/status`: connection count and session-ID listing; still needs restriction.

Website adapter uses `GET /api/product` and `GET /api/product/{id}`. Prices, variants,
images and gift information are mapped from product responses. Categories were
previously discovered at `/api/categories`, but the adapter does not provide a
separate category service. Sales-screen categories remain locally defined.

No verified negotiated-B2B-pricing service or certification-evidence API is integrated.
The frontend must still render images, actions and QR links, handle microphone/TTS
permissions, and implement presence detection, attract mode, quiet mode and reconnects.

## 5. Verification performed for this report

| Check | Result |
| --- | --- |
| Git branch, latest commits, staged/unstaged/untracked files | Inspected; working tree is not clean. |
| Current full test suite | **181 passed, 1 failed**, 39.60 seconds, one deprecation warning. |
| Dependency consistency (`pip check`) | **No broken requirements found.** |
| Current production database writes or paid model evaluation | Not performed. |
| Production deployment / GitHub Actions execution | Not performed or verified. |
| Fresh security-advisory scan | Not performed; earlier JSON advisory reports are historical evidence only. |

For tests, process-local overrides selected mock inference, JSON product mode,
memory checkpointing, a blank operational MongoDB URL and development auth settings.
The on-disk `.env` was not edited. The full suite is **not strictly offline**:
`test_contract_greenfibre_live_repo_schema` directly reads the public website API.
Some tests also load local embedding artifacts. The suite should eventually separate
live integration checks from deterministic CI tests.

**Failing test:**
`tests/test_brain_reliability.py::test_unapproved_certification_is_rejected_even_if_in_tool_description`

It expects the statement `It is certified food safe.` to be rejected when the only
support is an unapproved tool string containing the same words. The current validator
accepts it. Matching words are not proof of an approved certification for a SKU.

The warning concerns the existing `langchain-community` deprecation. It is not the
cause of the failing test and does not justify blindly upgrading all dependencies.

## 6. Confirmed gaps and release blockers

### High priority

1. **Certification evidence is not enforced correctly.** The failing regression
   confirms the validator can accept an unsupported claim. `app/brain.py` uses text
   matches and the absence of an empty-certifications string as evidence, rather
   than a validated, approved SKU record.
2. **Customer numbers can validate business claims.** The factual validator adds
   numbers from `user_message` to allowed values. A customer budget or requested
   quantity must not authorize a product price or actual inventory assertion.
3. **The sales engine reintroduces unsafe defaults.** `app/sales_engine.py` contains
   `in_stock=True` fallbacks, `GF:PROD` fallback identifiers and a default quantity
   of 50 for contact-collection actions. Product cards come from catalogue snapshots,
   not necessarily fresh detail checks. Missing prices can be rendered as the string
   `None`; budget filtering can retain products whose price is unknown.
4. **Contact presence is treated as consent.** A phone-number match can set consent
   to granted. A number in conversation is not necessarily permission to contact.
   Same-turn refusal and consent revocation need explicit precedence and evidence.
5. **Consent/lead helpers are not a complete workflow.** Storage functions exist,
   but no application caller was found for `record_customer_consent` or
   `record_sales_lead`. Lead IDs are random, with no request-level idempotency.
   Repeated consent insertion can conflict with the unique session/type index.
6. **Raw conversation privacy remains unresolved.** Operational message storage
   writes the full user/assistant text. Masking phone fields in a lead record does
   not redact contact details from conversations, checkpoints or tool output logs.
7. **Multiworker security needs hardening.** Ticket replay records and session
   activity are process-local. Static shared-secret headers still allow the holder
   to supply a kiosk identity. That is a trusted-gateway capability, not independent
   per-device authorization, and should be restricted accordingly.
8. **Persistence is not fully hardened.** Idle-expiry timestamps are not restored
   across process restarts. The custom saver has no TTL indexes for checkpoint,
   blob and write collections. `list()` drops kiosk identity when reading tuples;
   `put_writes()` lacks its own ownership check. These are saver-interface gaps,
   not evidence of a demonstrated public exploit.

### Medium priority

9. **Cancellation is only partial.** The WS loop awaits a complete turn before it
   reads another client message. A same-connection cancel event cannot reliably
   interrupt ongoing inference. Replies are generated fully before word emission;
   this is not provider-token streaming.
10. **Duplicate execution risk remains.** The WS TypeError compatibility fallback
    invokes `ask_avatar_stream` again, which can repeat a turn if the TypeError came
    from inside the first execution.
11. **Release protections are incomplete.** No shared rate limiter or connection
    cap was found. `/ws/status` exposes session IDs. Some exception paths still log
    raw exception content; structured tool latency and LLM usage are incomplete.
12. **Sales coverage is partial.** Rules are mostly English keyword/regex based.
    Full Hindi/Hinglish intent handling, objection handling, delivery qualification,
    robust variant choice and distinct household routing require more coverage.
    Customer-name extraction currently computes a value without persisting it through
    the normal turn path, despite stronger claims in some comments.
13. **Documentation and test claims need reconciliation.** Older reports cite
    140 or 178 passing tests and live checks. Those are historical claims, not the
    current 182-test result. Some frontend examples lack newer authentication or
    suggest parsing text despite structured action support.

## 7. Git and delivery status

The working branch contains a mixture of staged changes, unstaged changes to the
same files, and untracked application modules, tests and documents.

Notable untracked application files at review time:

- `app/database.py`
- `app/mongo_checkpointer.py`
- `app/sales_engine.py`
- `app/schemas_mongo.py`
- `app/storage.py`

These are real dependencies of the current working application. A commit of only
the already-staged files would not necessarily reproduce the behavior reviewed here.
Review and stage a coherent set after fixing regressions. Do not blindly include
`scratch/`: inspect generated files for secrets, customer data and temporary results.

This review did not stage, commit, push, merge or deploy anything. The open
`.git/COMMIT_EDITMSG` tab alone does not establish that a commit completed.

## 8. Recommended next milestones

1. Fix the certification regression and remove customer-input numbers as evidence
   for prices/stock. Add explicit per-SKU, per-field provenance tests.
2. Make UI actions use the same safe repository views and freshness rules as tools;
   remove invented SKU, stock and quantity defaults.
3. Implement explicit consent confirmation/refusal/revocation, data minimization,
   contact redaction and idempotent lead storage before enabling real submissions.
4. Harden shared ownership/replay/expiry, custom checkpointer semantics and retention;
   test concurrent workers and restart behavior against an isolated test database.
5. Separate WS receiving from turn execution for real interruption; add abuse limits.
6. Separate offline tests from live integrations, resolve all failures, reconcile
   documentation/OpenAPI, then commit a complete reviewable change set.
7. Integrate and test the actual frontend: authentication, product cards, actions,
   QR display, audio interruption, language switching and idle/quiet behavior.
8. Perform an explicitly approved staging review before production deployment.

## 9. Practical conclusion

The main backend pieces are now in place and most current tests pass. The remaining
work is primarily correctness, security, privacy, integration and release discipline.
The project should be described as **a working backend under active integration and
hardening**, rather than a finished production kiosk or a completed CRM/checkout system.
