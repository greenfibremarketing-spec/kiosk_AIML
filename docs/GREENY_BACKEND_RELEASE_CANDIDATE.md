# Greeny backend release candidate

> Historical reliability audit: the current guided-flow source, test results and handoff are documented in [GREENY_FRONTEND_CONTRACT_FINAL.md](GREENY_FRONTEND_CONTRACT_FINAL.md). Later coordinator and guided-flow changes supersede the implementation details below. The source manifest identifies the current tested working tree.

Review date: **9 October 2026**. Branch: **feature/greeny-reliability-fixes**.
Base commit: **4163bf2** (`Add Greeny architecture audit and validated sales state`).
This is an **uncommitted, staged proposal**, not a deployed release. It supersedes
backend acceptance/readiness claims in the earlier reports, including the status
report that recorded 181 passed / 1 failed. No frontend files were changed.

## Decision

**Ready for backend code review and isolated acceptance testing; not production-ready.**
The deterministic regression suite has zero failures. Production data, leads,
orders, database permissions and deployments were not accessed or modified.
Real MongoDB behavior, approved retention policy and legacy-data handling remain
release gates. Passing fake-database tests is not proof of Atlas readiness.

## Verification

Final verification results and source hashes are recorded below and in
`GREENY_BACKEND_SOURCE_MANIFEST.json`. The manifest hashes the proposed backend
Git blobs, so it remains useful even when Windows line endings differ.
`GREENY_BACKEND_TEST_ENVIRONMENT.json` records the installed Python/package versions.
`requirements-tested-constraints.txt` captures that tested environment; dependencies
were not upgraded during this work.

- Full deterministic suite: **214 passed, 0 failed, 1 skipped**.
- The skipped test is `test_contract_greenfibre_live_repo_schema`, the existing
  public website integration check. It requires explicit `--run-live`.
- One warning: the existing `langchain-community` deprecation in `app/rag.py`.
- `python -m pip check`: **No broken requirements found**.
- `git diff --check` and staged whitespace checks: must pass before handoff.
- No live website, LLM, MongoDB integration or remote GitHub Actions run was
  performed. The CI advisory scanner has not been rerun locally; dependency
  compatibility is not a claim of zero known vulnerabilities.

The default suite forces mock LLM, JSON products, memory persistence, blank Mongo
credentials and development authentication before application import. Tests stub
embedding warmup and graph retrieval, and block non-local socket connections.
RAG unit tests still exercise their own injected stores. This is not a live model,
embedding-quality or multilingual sales-quality evaluation.

## Confirmed findings and file-by-file changes

| Severity | Files | Resolution / evidence |
| --- | --- | --- |
| Critical | `app/brain.py`, `app/factual_safety.py` | Tool prose, a list of certifications, matching words or a self-declared `approved` flag no longer authorize compliance claims. No approved SKU evidence registry exists: these claims fail closed. The original failing regression now passes. Unsafe final model replies are replaced before checkpointing. |
| High | `app/brain.py` | Customer budgets/quantities, RAG and product descriptions no longer populate a shared number whitelist. Product price/MRP and known stock are separate fields; price cannot prove stock, or vice versa. Historical messages are redacted before model use. |
| High | `app/sales_engine.py`, `app/product_repository.py`, `app/tools.py` | UI candidates are refreshed through the repository. Unknown, stale, inactive and out-of-stock live products are excluded from recommendations. No fabricated SKU, available-stock or 50-unit fallback. Missing prices fail budget matching. Selected variant identity is preserved; snapshot variant stock is null. Cards expose source/freshness status. Compliance text is withheld from descriptions. JSON fixtures remain mock-only. |
| Critical | `app/sales_state.py`, `app/sales_engine.py`, `app/brain.py` | Phone presence never grants consent. A scoped confirmation precedes collection; yes works only with pending confirmation. Explicit refusal/revocation take precedence, including over human handoff. Bare phone numbers cannot restore revoked permission. |
| High | `app/privacy.py`, `app/storage.py`, `app/callbacks.py`, `app/mongo_checkpointer.py` | Phone/email redaction before model input, conversation writes, tool traces, checkpoint blobs/writes and checkpoint reads. Consent operations have deduplication IDs. Leads remain idempotent local drafts; stored current scoped consent is required for a masked contact association. Revocation removes draft contact associations. No external lead submission is implemented or performed. |
| Critical | `app/mongo_checkpointer.py`, `app/coordination.py`, `app/sessions.py` | Atomic immutable owner claims use Mongo `_id`; missing/mismatched identities are rejected on reads, listing, writes and deletion. Returned checkpoint configurations retain kiosk identity. List metadata filters/limits are honored. Shared turn exclusion prevents simultaneous backend turns/reset. Persistent last activity supports restart expiry. Reset retains the owner binding. |
| High | `app/mongo_checkpointer.py`, `app/database.py` | Unique checkpoint/blob/write indexes and retention indexes defined; pending writes receive timestamps. Expired checkpoints fail closed before the TTL monitor runs. Blob timestamps are refreshed when referenced by a new checkpoint. Index provisioning is explicit, not an automatic application-start mutation. Production startup validates checkpoint indexes. |
| Critical | `app/server.py`, `app/coordination.py` | Production static shared-token impersonation removed; signed kiosk identity required. Ticket consumption, request limits and connection slots use shared Mongo records across workers. `/ws/status` requires gateway authentication and never lists session IDs. HTTP bodies, WS frames, connections and inference admission are bounded. Error payloads omit exception details. |
| High | `app/server.py`, `app/brain.py`, `app/config.py` | WS receive loop runs separately from inference. Cancellation is acknowledged while inference is blocked. Canceled output is suppressed, subsequent concurrent turns are rejected, duplicate `turn_id` is rejected, and internal TypeError never retries without kiosk identity. Provider calls have a configured timeout and SDK retries disabled; existing bounded application retry policy remains. |
| Medium | `tests/conftest.py`, `tests/test_release_candidate.py`, existing regression tests, `.github/workflows/ci.yml` | Offline/live separation, new security/privacy/concurrency tests, corrected unsafe historical expectations, CI JUnit artifact. Existing dependency/advisory checks retained. |
| Documentation | `.env.example`, `docs/openapi.json`, release report, environment inventory, constraints, source manifest | Reproducible backend handoff without credentials or production operations. |

## Test evidence by requirement

- **Product factual safety:** original certification regression; tool-description and
  self-asserted certification rejection; preferences cannot authorize price/stock;
  price versus stock separation; fresh price changes; missing price/SKU; inactive,
  unknown, expired and zero stock; variant identity and safe descriptions.
  See `test_brain_reliability.py`, `test_greenfibre_repository.py`,
  `test_product_tools.py`, and `test_release_candidate.py`.
- **Consent/privacy:** anonymous phone entry, pending scoped yes/no, same-message
  refusal, revocation followed by phone entry, replayed grant after revocation,
  duplicate draft leads, stored consent requirement, callback/message redaction,
  serialized Mongo checkpoint/blob/pending-write redaction.
- **Database/authentication:** isolated Mongo saver restore, actual LangGraph running
  against the fake saver across restart, competing owner claims, cross-kiosk
  read/list/write/reset denial, shared replay/rate/mutex state, restart idle expiry,
  TTL/uniqueness definitions, read-side expiry, gateway-only status and production
  static-token rejection. These tests do not exercise a real Mongo server.
- **WebSocket:** receive cancel while the fake provider is blocked, truthful
  cancellation metadata, no stale token/action/done, no duplicate invocation,
  no TypeError retry, connection cap, duplicate connection denial, v1/v2 compatibility.

Some old assertions deliberately changed: arbitrary certification strings are now
rejected; quoting first asks permission rather than collecting a number; stale
variant stock is null; stored consent replaces caller-only boolean authorization.
The 20-turn fixture now injects explicit test SKUs rather than relying on fabricated
application defaults. These changes strengthen the contract rather than hiding
regressions.

## Updated API and event contracts

Existing `/chat`, `/v1/chat`, `/products`, `/session/reset`, `/v1/session/reset`,
`/health`, `/auth/kiosk/token` and `/ws/{session_id}` remain. OpenAPI is regenerated
in `docs/openapi.json`. WS is documented here because OpenAPI does not describe it.

- Production requires Mongo persistence, a kiosk signing secret, a separate gateway
  credential and an explicit kiosk allowlist. The gateway issues short-lived signed
  tickets; the signing secret is not a kiosk-client credential. Dev static-header
  authentication remains for local compatibility. Never put permanent credentials
  into URLs. WS ticket consumption is shared across workers.
- `GET /ws/status`: gateway credential required (`X-Gateway-Key` or gateway Bearer).
  Response: `{"active_connections": 0, "scope": "this_worker"}`. No session IDs.
- Oversized HTTP body: 413; message/schema validation: 422; request limit: 429 with
  Retry-After; concurrent REST turn/reset: 409. Existing generic 500/503 failures
  remain. WS connection capacity rejection: close 1013; identity/frame limits: 1008.
- Optional WS input `turn_id` is 1-64 ASCII letters, digits, underscore or hyphen.
  Supply a stable ID on retries: `{"message":"hello","turn_id":"turn-001"}`.
  IDs are deduplicated for 24 hours. Legacy clients without IDs remain supported,
  but disconnected retries without a stable ID cannot be deduplicated.
- `{"type":"cancel","turn_id":"turn-001"}` can arrive during inference.
  v2 acknowledgement: `{"v":2,"type":"cancelled","data":"","meta":{
  "turn_id":"turn-001","provider_cancelled":false,"output_suppressed":true}}`.
  Sync provider work/checkpoint updates may still finish. The backend keeps turn
  exclusion until work actually exits; it does not claim provider cancellation.
  No automatic replay or second execution is performed.
- New action `CONFIRM_CONTACT_CONSENT` includes
  `consent_scope: "quotation_contact_only"`, `submission_enabled: false`, and known
  quantity or null. `COLLECT_PHONE` occurs only after explicit confirmation, and
  remains marked submission-disabled. Conversation reply asks the scoped question.
  Frontend implementation of this new action remains a separate integration task.
- `SalesState.contact_consent_pending` is an additive boolean. Consent state is
  `not_asked`, `granted`, `declined` or `revoked`. Contact submission/CRM writes are
  not enabled by the new action. Internal consent records preserve explicit status.
- Product cards carry canonical SKU/variant identifiers. Unknown prices/stock are
  null, not zero or strings such as `"None"`. Fresh API detail verification is used
  for recommendation actions. Existing quote/website handoff tools also re-read
  detail; retail stock is an observation, not an inventory reservation.
- Implemented website reads remain `/api/product?page=...&limit=...` and
  `/api/product/{id}`; the website has not been contacted to revalidate them in this
  handoff. Certifications, negotiated B2B prices and checkout/order creation have
  no approved integration contract and are not invented.

## Local VS Code / PowerShell reproduction

Use Python 3.13. The existing `.venv` was tested. For a separate clean environment:

```powershell
cd "C:\Users\Shivansh Dubey\OneDrive\Desktop\Kiosk_AIML"
py -3.13 -m venv .venv-rc
.\.venv-rc\Scripts\python.exe -m pip install -r requirements.txt -c requirements-tested-constraints.txt
.\.venv-rc\Scripts\python.exe -m pip check
.\.venv-rc\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
```

The constraints record the tested Windows environment. Installation in a fresh
virtual environment and on Linux was not performed here; CI must confirm Linux
resolution. No activation or `.env` edits are required for pytest. For the existing
venv, replace `.venv-rc` with `.venv`. Tests do not use production credentials.

Run the original failure directly:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_brain_reliability.py::test_unapproved_certification_is_rejected_even_if_in_tool_description -q
.\.venv\Scripts\python.exe -m pytest tests/test_release_candidate.py -q
```

For a local API smoke test in a new terminal, explicitly override developer `.env`:

```powershell
$env:LLM_PROVIDER="mock"
$env:PRODUCT_SOURCE="json"
$env:CHECKPOINTER_BACKEND="memory"
$env:GREENY_AI_MONGO_URL=""
$env:ENVIRONMENT="development"
$env:KIOSK_AUTH_SECRET=""
$env:KIOSK_GATEWAY_KEY=""
$env:HF_HUB_OFFLINE="1"
$env:TRANSFORMERS_OFFLINE="1"
.\.venv\Scripts\python.exe -m uvicorn app.server:api --host 127.0.0.1 --port 5007 --ws-max-size 24256 --ws-max-queue 4
```

JSON product fixtures deliberately lack live inventory and some SKUs. They do not
represent commercial availability; the new sales tests inject explicit fixture IDs.
The API may log a RAG warmup warning if embedding artifacts are not cached.
Live read-only website checking is separate and optional:
`python -m pytest tests/test_frontend_contract_audit.py -m live --run-live -q`.
This was **not executed**. There is no live database test enabled by that flag.

## Persistence / retention operations and remaining blockers

1. **Real Mongo acceptance is outstanding.** On an explicitly approved isolated
   database, provision `MongoCheckpointSaver.ensure_indexes()` and
   `create_indexes_for_greeny_ai(db)` using the deployment identity. Inspect existing
   duplicates/index conflicts before creating unique indexes. Do not run this against
   production as part of local tests. Check real concurrency, TTL monitoring, process
   restart, replica/network failures and backup restore. Production startup verifies
   checkpoint uniqueness/TTL definitions; operations must also verify operational TTLs.
2. **Crash recovery is fail-closed, not automatic lease stealing.** Mutex records have
   no TTL: an old provider thread must never resume after another worker took over.
   A crashed worker can leave a session/inference/connection slot locked. Recovery
   requires stopping the affected worker fleet, proving no old inference can resume,
   and then removing only the abandoned mutex records through an approved operator
   process. Do not delete locks while workers are alive. Normal completion releases
   locks; checkpoint/session state survives clean restarts. Automated fenced recovery
   and fault-injection acceptance remain production work.
3. **Retention approval is missing.** The existing configurable retention value is
   90 days, with matching operational/checkpoint TTL definitions. This is a configured
   value, not evidence of an approved policy. Consent operation IDs expire with that
   window. Owner tombstones remain to prevent recycling a known session into another
   kiosk; replay/rate records expire separately. Approve retention of those metadata
   records, backups, masked contacts and logs before deployment.
4. **Legacy raw data is not migrated.** New writes and reads redact phone/email, but
   earlier raw records/backups may remain. Read redaction is not physical erasure.
   Inventory/purge historical data only under approved retention/migration procedures.
   Pattern redaction is not a general PII detector (e.g. arbitrary postal addresses,
   indirect identifiers or obfuscated contacts). Keep contact submission disabled
   until approved privacy handling is complete.
5. **Approved SKU compliance evidence is absent.** Certification/safety assertions
   are disabled rather than trusting merchant descriptions. An approved, scoped,
   expiring evidence contract and its ingestion/revocation workflow are prerequisites
   for enabling those claims. Natural-language guards are conservative; adversarial
   live-model and multilingual acceptance still need review.
6. **Live product/LLM and frontend acceptance remain.** Verify actual API contract,
   latency, price changes and rate limits in a read-only environment. Verify frontend
   consent prompts, busy/cancel handling and stable turn IDs without changing frontend
   code in this proposal. Stock is never a reservation and checkout still requires
   website confirmation. No real leads/orders should be submitted.
7. **Deployment operations remain.** Run the CI security/advisory scan, secret review,
   Linux clean install and proxy/load tests. Configure HTTPS/WSS, edge request/idle
   timeouts, transport WS limits and restricted network access. Do not forward arbitrary
   proxy headers as identity. Confirm gateway/signing credentials and least-privilege
   database permissions through the authorized deployment process.

## Proposed commit and exclusions

Suggested title: `Harden Greeny factual safety, consent, persistence and websocket turns`.
The staged proposal includes required existing untracked backend modules, tests,
configuration, knowledge corrections and this handoff. No commit or push was made.
Scratch output and historical untracked handoff/acceptance reports are preserved
outside the proposal; they are not runtime dependencies and are not fresh acceptance
evidence. `.env`, virtual environments, caches and real customer data are excluded.
Use `git diff --cached --stat`, `git diff --cached` and the source manifest to review
exactly what would be committed. Deployment and database operations require separate
approval; this report does not authorize either.
