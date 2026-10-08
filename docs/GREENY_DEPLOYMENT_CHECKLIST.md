# Deployment readiness: blocked pending security and integration work

This checklist supersedes any interpretation that the current launch examples
are production-ready. No deployment settings or services were changed in this patch.

## Verified locally

- [x] Legacy successful HTTP/WS contracts exercised with offline fixtures.
- [x] Missing stock/price no longer converted into invented values by product tools.
- [x] Reset deletion errors are surfaced safely.
- [x] Public endpoint exception text and validation input echo removed.
- [x] Full-length anonymous UUIDs and application message limits added.
- [x] Offline regression suite and dependency consistency checks pass.
- [x] Dated installed-package advisory scan passes after targeted pip update.
- [x] CI workflow added; no remote execution or deployment performed.

## Required before public staging / production

- [ ] Enforced API/WS authentication with configured kiosk principals and rotation.
- [ ] Session ownership on chat/reset/WS; no session-ID enumeration.
- [ ] Shared rate limits, request body and WS frame caps at application/proxy layers.
- [ ] Connection/session caps and bounded histories; cancellation and per-session locks.
- [ ] Persistent supported LangGraph checkpointer plus ownership/expiry repository.
- [ ] Tested restart recovery, multiworker consistency, purge and retention policy.
- [ ] Trust-approved product source and channel pricing/stock/MOQ service semantics.
- [ ] Missing/stale business facts fail closed; JSON is explicitly development only.
- [ ] Approved per-SKU safety/certification evidence and versioned policy documents.
- [ ] Structured per-SKU claim verification and safe checkpointed assistant history.
- [ ] Provider timeouts, bounded retries, SDK retry review and outage behavior.
- [ ] Consent evidence, contact minimization, approved CRM and idempotent submissions.
- [ ] Allowed checkout/human-handoff endpoints; no autonomous orders/payments.
- [ ] Redacted callback logs and structured kiosk/tool/LLM usage telemetry.
- [ ] TLS proxy, trusted origins, secret storage, rotation of previously exposed keys.
- [ ] Trusted FAISS artifacts only; avoid deserializing user-controlled pickle files.
- [ ] Reproducible dependency constraints and reviewed community integration migration.
- [ ] CI runs on the feature branch; 30+ sales scenarios and optional model evaluation.
- [ ] Health/readiness separation, metrics/alerts, load tests and rollback plan.
- [ ] Explicit approval for production resources, writes, push, merge and deployment.

Remaining business inputs: inventory freshness SLA, channel-price interpretation,
MOQ/discount authorization, approved certifications, policies and contacts, consent
wording, CRM schema, checkout contract, escalation destination and retention period.

Do not use the existing two-worker launch example with MemorySaver as a durable
production configuration. This patch deliberately leaves that configuration alone
until persistence and ownership can be implemented and tested together.
