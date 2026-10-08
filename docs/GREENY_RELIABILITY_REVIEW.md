# Greeny reliability review: first implementation milestone

Date: 2026-10-08. Starting branch: `feature/greeny-sales-foundation`, clean and
tracking its origin branch. Latest commits inspected: `f6c0d01`, `efa6f43`,
`befd8d8`, `dff901d`, `5ee7b34`. Work is on `feature/greeny-reliability-fixes`.
No fetch/push, production database access, merge or deployment was performed.

## Actual implementation status

The earlier architecture audit remains a useful baseline, not a list of completed
features. At the start, only the standalone sales-state contract and its tests had
been added to the runtime project. HTTP/WS still call the legacy LangGraph agent;
the state model is not yet attached to that graph. MongoDB chunks remain independent
of FAISS policy retrieval and JSON catalogue tools. Existing working infrastructure
was reused. Baseline: 39 offline tests passed; `pip check` passed.

## File-by-file audit and disposition

| File / area | Severity | Confirmed defect or missing feature | This milestone |
| --- | --- | --- | --- |
| `app/tools.py` | High | Missing stock defaults to available; missing prices may become zero; gift minimum hardcoded; partial/empty IDs pick first product | Fixed defaults, status handling, budget validation, exact lookup and pricing fallback; preserve tool names/signatures. |
| `app/product_repository.py` | High integration gap | No replaceable product data boundary | Added read-only Protocol and JSON adapter. No MongoDB adapter enabled. |
| `app/server.py` | High | Raw exception details; raw WS message logging; repeated turn on WS error; malformed JSON values | Generic errors, no message logging, no full-turn retry, safe validation and recovery. |
| `app/server.py` | High | Short session IDs; no input bounds | Full UUID identifiers; 4,000-character message default, session-ID validation, WS application bounds. Raw HTTP body cap and network WS frame cap remain separate requirements. |
| `app/server.py` | Critical deployment blocker | No API authentication, trusted kiosk identity, session ownership or rate limits | Still missing. Public deployment is not approved by this milestone. |
| `app/sessions.py` | High | Reset swallows checkpoint deletion errors | Fixed; failed reset retains activity metadata and returns HTTP 503. |
| `app/sessions.py` | High | Memory only, expiry on access only, no namespace/retention/concurrency control | Still missing; no claim of production persistence or isolation. |
| `app/brain.py` | High | Blanket material/safety claims; mock truncates prices and invents missing facts; accepts tiny unverified prices; old tool evidence validates new turn | Removed blanket prompt/mock claims, preserved decimal prices, removed tiny-value exemption, restricted tool evidence to latest turn. |
| `app/brain.py` | High | Number-set matching is not SKU/field verification; RAG validation depends on tracing; rejected text remains checkpointed; no overall provider deadline | Still open. Prompt constraints are not a factual security boundary. |
| `app/rag.py`, `data/knowledge/*` | High | Policy text labelled verified without approval evidence; broad certifications; trusted pickle load; possible prompt injection | Reviewed, not migrated. Existing documents remain unapproved for production. |
| `data/products.json` | High for live use | Static prices/stock and descriptive safety assertions | Source unchanged. Tool views mark price as a development snapshot, suppress unapproved certification field, require recent stock observation. Descriptions are not certification evidence. |
| `app/product_chunks.py`, `scripts/ingest_mongo.py` | Medium | Snapshot prices/stock, all activity states, no atomic write batch or deleted-product purge | Existing text ingestion left unchanged and not executed. |
| `app/config.py` | Medium | No authenticated identity/persistence/retention configuration | Added validated `MAX_MESSAGE_CHARS` only. |
| `app/callbacks.py` | High privacy / Medium correctness | Raw argument/result previews; parallel tool timings can be misattributed; missing usage accounting | Open. HTTP now logs JSON metadata with server-generated request IDs; tool/LLM telemetry requires separate redesign. |
| `app/sales_state.py` | Feature gap | Validated fields but no runtime routes, external consent evidence or lead authorization | Existing contract preserved. |
| `requirements.txt` | Medium | Broad lower bounds, no reproducible lock | All installed distributions scanned; no wholesale upgrades. Current transitive dependency on text splitters and community-package deprecation need resolution. |
| `.github/workflows/ci.yml` | Feature gap | No automated checks | Added offline tests, dependency consistency check and isolated advisory scan with failure gate. CI has not run on GitHub. |
| `tests/` | High coverage gap | No tool/API/reset regression tests | Added 42 cases beyond the 39 baseline, including real graph execution with mock LLM and stubbed RAG. |
| `DEPLOYMENT_UBUNTU.md` | High | Multiworker examples use process-local sessions | Configuration unchanged; release checklist explicitly blocks this setup. |

## Behavior and compatibility changes

- Existing REST paths, successful chat fields, WS token/done/error events and tool
  names remain. HTTP responses additionally carry `X-Request-ID`.
- Error messages become generic; validation responses omit submitted input.
- Session IDs now accept 1–128 ASCII letters/digits plus `_ . : -`; callers with
  other IDs must migrate. New generated IDs have a full UUID rather than six hex digits.
- Message length is configurable through `MAX_MESSAGE_CHARS` (default 4,000).
- Product tools add source/status fields; missing price/stock is null. `/products`
  retains its legacy raw snapshot shape and must not be treated as live inventory.
- Positive/zero stock needs a timezone-aware `stock_checked_at` observation within
  five minutes, consistent with `in_stock`. Missing/stale/conflicting observations
  yield unknown; inactive is distinct from absent and out-of-stock. The timestamp
  alone is not authority verification: this is still a development adapter.
- JSON catalogue is reread per tool request to remove an indefinite stale cache.
  Legacy missing activity flags are accepted only in this development schema.
- WS failure now returns one error rather than executing the same turn again.
- Mock policy replies ask for confirmation instead of repeating unapproved delivery,
  safety and MOQ claims. Existing live smoke-test expectations need updating; they
  were not run because they may call paid services/download embeddings.

## Prioritized next changes and review gates

1. **Authentication and ownership, before exposure.** Optional dev mode plus fail-closed
   production mode; server-side credentials map to kiosk/channel principals. Check
   ownership on chat, reset and WS. Do not trust a caller-supplied kiosk header.
   Browser WS needs a short-lived, scoped connection ticket or trusted proxy; never
   embed permanent API keys in public frontend JavaScript or URL query strings.
   Remove session-ID enumeration. Add rate limits, raw body/frame bounds and caps
   on active sessions/connections with shared deployment enforcement.
2. **Persistence and concurrency.** Select a supported PostgreSQL LangGraph saver,
   separate ownership/expiry records, transactions and per-session serialization.
   Preserve MemorySaver for dev. Tests: restart, cross-kiosk denial, concurrent reset,
   expiry, purge and multiworker recovery. A checkpointer alone does not solve ownership.
3. **Fact authority.** Typed repository/business-fact adapters with source verification,
   per-channel pricing, currency, observation time, SKU status, MOQ and approved
   certification registry. Keep snapshots for development; never silently fall back
   on an authoritative-service outage. Rewrite factual validation around structured
   claims; ensure corrected replies, not rejected text, enter memory.
4. **Sales workflow.** Integrate existing SalesState in LangGraph with explicit B2B/D2C
   routes, one qualification question at a time, up to three relevant options,
   objection handling, quote estimates, consent evidence and mock business services.
   Backend enforces pricing and idempotent leads; LLM cannot authorize discounts,
   payments, submissions or data edits. Human and checkout handoffs require approved
   endpoints. No production submissions during development.
5. **Engagement and telemetry.** Implement the event design in
   `GREENY_KIOSK_ENGAGEMENT_DESIGN.md`; add authenticated kiosk/request context,
   tool run-ID latency and provider usage totals without prompts/results/contact text.
6. **Evaluation and release.** At least 30 conversation scenarios, including multilingual
   intent, B2B, refusal, duplicate leads, unsupported certifications and tool outages.
   Keep model-based evaluations separate from deterministic CI. Approve staging and
   operational checklist before any production writes, push, merge or deployment.

## Test and dependency evidence

- Milestone A (repository/status/reset): **65 passed**.
- Milestone B (API/mock): **79 passed**.
- Real graph regression initially failed on `mug?`; punctuation tokenization was fixed.
- Final regression suite: **81 passed**, one existing community-package deprecation warning.
- `pip check`: no broken requirements. Runtime packages were not upgraded.
- Initial pip-audit 2.10.1 scan: 108 distributions; 12 advisory entries (6 unique IDs)
  for pip 25.0.1, no skipped packages. Full before/after JSON reports are alongside
  this document. Only `.venv` pip was upgraded to 26.2.1; scanner installed in a
  separate ignored `.venv-security` environment.
- Rescan: **no known vulnerabilities found**. This is a dated advisory result,
  not a guarantee against unknown vulnerabilities or application defects.
- GitHub CI added locally but not executed remotely; dependency versions resolved
  by CI can differ until a reviewed lock/constraints strategy is adopted.

Advisory method: [PyPA pip-audit](https://github.com/pypa/pip-audit).
Validation handling follows [FastAPI error handling](https://fastapi.tiangolo.com/tutorial/handling-errors/).
No production readiness claim is made. This milestone intentionally stops before
larger security architecture and sales features.
