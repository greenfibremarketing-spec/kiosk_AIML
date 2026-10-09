# Greeny AI final frontend contract

**Audit:** 9 October 2026. **Branch:** `feature/greeny-reliability-fixes`.
**Base:** `1c3b6d5` plus the current uncommitted working tree.
**Scope:** existing FastAPI + LangGraph + WebSocket backend; no frontend changes.
This document supersedes conflicting guided-flow payloads and completion claims in
`GREENY_GUIDED_CONVERSATION_IMPLEMENTATION.md`. It is a local contract sign-off,
not production deployment approval.

## 1. Comparison and confirmed corrections

The frontend team's original guided-flow specification was not available in the
repository or supplied attachments. A request for its location was made. The older
attached project brief supplies the eight-stage sales outline, not an original
frontend command specification. **Full comparison against that missing source is
outstanding.** This audit verifies the user's ten listed requirements, the supplied
implementation guide, and the actual local routes and code.

| Guide / implementation before audit | Verified correction |
| --- | --- |
| `SET_NAME` documented, only `submit_name`/`name` translated | Canonical `SET_NAME` is validated and implemented on REST and WS. |
| `SHOW_ALL` documented, not translated by either transport | Validated and implemented on both transports. |
| Free-form action and unbounded name/category/SKU fields | Shared Pydantic contract; command-specific fields, length/type bounds, mutually exclusive message/action. |
| `SPEECH_DONE` could jump from any stage back to name entry | Must match pending greeting, session, speech ID and a new event ID. Duplicate, wrong-session and stale acknowledgements are rejected. |
| Guide proposed `?ticket=` / `X-Kiosk-Ticket` | Actual names are `?token=` / `X-Kiosk-Token`, or Bearer authorization. |
| Fixed categories included unsupported office/corporate groupings | Options are derived from currently eligible repository products; exact category matching, no unrelated fallback. Corporate intent is a sales route, not a fabricated product category. |
| `GF:BTL:VIORA` used as an integration example | Replaced by IDs actually read from the configured website repository. |
| New stage enums advertised while responses used old stage names | Guided sessions now emit the documented guided stage names. Legacy non-guided conversational states remain supported. |
| Restart cleared selected fields but retained conversation history | Restart resets LangGraph session history and sales preferences, preserves kiosk ownership, and issues a new greeting token. |
| API errors became empty catalogues / left stale UI ambiguity | Product-service failures produce explicit 503 / WS error with fact-invalidation metadata. |
| "259 passed, no skipped, --run-live" was claimed | Replaced by the exact final results below. No production frontend, LLM or database acceptance is implied. |

`/api/flow/step` and `/api/flow/interpret` **do not exist**. Tests verify POST returns
404 for both. Do not call them. No replacement backend framework or `/api/flow`
routes were added.

## 2. Endpoints and authentication

Use the approved deployment origin supplied by operations. Local testing used
`http://127.0.0.1:<ephemeral-port>` and `ws://127.0.0.1:<ephemeral-port>` with a real
Uvicorn server. The production hostname in the old guide was not deployment-tested.

| Endpoint | Purpose | Authentication |
| --- | --- | --- |
| `POST /auth/kiosk/token` | Trusted gateway issues signed kiosk ticket | `X-Gateway-Key` or gateway Bearer; browser must not hold the gateway/signing secret |
| `POST /chat`, `POST /v1/chat` | Text/transcript or guided command | `X-Kiosk-Token` or Bearer; signed kiosk ticket in production |
| `WS /ws/{session_id}?v=2` | WS v2 conversation and control frames | Same ticket via header or `token` query parameter; ticket is consumed on connect |
| `GET /products` | Catalogue snapshots | Public in the current implementation; not a stock/price verification endpoint |
| `POST /session/reset`, `/v1/session/reset` | Clear current LangGraph session | Kiosk ticket + session ownership |
| `GET /health` | Existing health endpoint | Current implementation is public |
| `GET /ws/status` | Worker connection count only | Gateway credential; no session list |
| `GET /coordinator/metrics` | Existing operational metrics | Gateway credential; not a frontend flow dependency |

Production requires shared persistence, configured authentication and kiosk
allowlisting. A claimed `kiosk_id` must match the ticket. Session ownership also
applies after reconnect/reset. Development without a configured secret is explicitly
unauthenticated local mode and must not be exposed as a production kiosk service.

Gateway request (sent by the trusted gateway, never by public frontend code):

```json
{"kiosk_id":"kiosk-01"}
```

The token endpoint returns `ticket`, `kiosk_id`, `expires_in` and
`ticket_type: "signed_ticket_v1"`. Use the returned values; do not construct tickets.
Browser WS example: `wss://<approved-origin>/ws/frontend-demo-01?v=2&token=<issued-ticket>`.
Use TLS, redact ticket URLs in infrastructure logs, and obtain a fresh ticket for
reconnect. `ticket=` and `X-Kiosk-Ticket` are not supported authentication fields.

## 3. Validated frontend inputs

All JSON is an object. `message` and `action` are mutually exclusive. Unknown fields
are rejected. Command names are uppercase, case-sensitive values listed below;
old unvalidated aliases are not part of this final contract.

REST adds `session_id` to every command. A message can omit it, in which case the
backend creates and returns one; persist that returned ID. WS obtains it from the
URL; if supplied in a frame it must match the URL. Optional `kiosk_id` must match
authentication. Optional `input_mode` is `text`, `voice` or `button` (default `text`).
Voice means an ASR **text transcript**, not audio bytes. Use opaque random session/event IDs; do not put customer names, phone numbers or emails into identifiers.

- `message`: trimmed string, 1 through configured `MAX_MESSAGE_CHARS` (default 4000).
- `turn_id`: required for commands; 1-64 ASCII letters, digits, underscore or hyphen.
- `speech_id`: 32 lowercase hexadecimal characters, exactly as issued for the greeting.
- `name`: 1-60 characters; letters, spaces, apostrophe and hyphen. Optional display
  name only; entering it does not grant contact permission.
- `category_id`: 1-100 characters; must be one of the currently returned category IDs.
- `sku`: canonical `GF:<product_id>` or `GF:<product_id>:<variant_id>`, at most 128
  characters; must identify a current repository product/variant.

### Speech completion and ASK_NAME

Start through REST (WS uses the same body without `session_id`):

```json
{"session_id":"frontend-demo-01","message":"hello"}
```

Greeting response shape (the sample speech ID is illustrative; use the actual
returned token, never this fixed example):

```json
{
  "reply":"Hi! I'm Greeny from Green Fibre. Welcome!",
  "session_id":"frontend-demo-01",
  "sales_stage":"GREETING",
  "action":{
    "action":"SHOW_CATEGORIES",
    "categories":[],
    "input_enabled":false,
    "awaiting_speech_done":true,
    "speech_id":"0123456789abcdef0123456789abcdef",
    "message":"Hi! I'm Greeny from Green Fibre. Welcome!"
  }
}
```

Despite the legacy screen-action name `SHOW_CATEGORIES`, this greeting envelope has
no categories and disables normal input. Render the welcome/avatar screen. Wait for
the complete matching response and the actual TTS `onend`; then send:

```json
{
  "session_id":"frontend-demo-01",
  "action":"SPEECH_DONE",
  "speech_id":"0123456789abcdef0123456789abcdef",
  "turn_id":"greeting-ack-001"
}
```

`SPEECH_DONE` is a **frontend command**. It has no message/name/category/SKU fields.
The backend checks identity, stage and token; it cannot independently prove audio
played. Timers, canceled speech and TTS errors are not completion acknowledgements.
A typed string `SPEECH_DONE` is not a valid substitute.

Successful REST response:

```json
{
  "reply":"What should I call you?",
  "session_id":"frontend-demo-01",
  "sales_stage":"ASK_NAME",
  "turn_id":"greeting-ack-001",
  "action":{
    "action":"ASK_NAME",
    "prompt":"What should I call you?",
    "skip_allowed":true,
    "message":"What should I call you?"
  }
}
```

`ASK_NAME` is a validated **backend screen action**, not a frontend command. Sending
`{"action":"ASK_NAME","turn_id":"x"}` is rejected. Show a name input, microphone
and explicit Skip button. No name or phone is required to shop.

### Remaining exact command bodies

These are complete REST request bodies. For WS v2, omit `session_id` (and optionally
add `"v":2`). Use a new `turn_id` for each new operation.

```json
{"session_id":"frontend-demo-01","action":"SET_NAME","name":"Noah","turn_id":"name-001","input_mode":"button"}
```

```json
{"session_id":"frontend-demo-01","action":"SKIP_NAME","turn_id":"skip-001"}
```

Both require `ASK_NAME` and lead to `ASK_CATEGORY` / `SHOW_CATEGORIES`. SET_NAME and
SKIP_NAME are alternatives, not sequential submissions of the same prompt.

```json
{"session_id":"frontend-demo-01","action":"SELECT_CATEGORY","category_id":"bottle","turn_id":"category-001"}
```

```json
{"session_id":"frontend-demo-01","action":"SHOW_ALL","turn_id":"all-001"}
```

Use emitted category IDs, not a static list. `SHOW_ALL` is a separate command with
no category/SKU field. The response is `SHOW_PRODUCTS`, with `products`, `count`,
and category metadata where applicable.

```json
{"session_id":"frontend-demo-01","action":"SELECT_PRODUCT","sku":"GF:6ac33bf78072ec78d51387f0:6ac33bf7e3e99cc6fbcfcc2b","turn_id":"product-001"}
```

This exact variant ID was retrieved from the configured Green Fibre repository.
The parent is `GF:6ac33bf78072ec78d51387f0`, **Viora Water Bottle - 400 ml**;
the variant is **Parrot green**. Select IDs from the response, not hardcoded examples.
`GREENY_FRONTEND_PRODUCT_IDENTITIES.json` records the actual public-read observation
and all 17 observed product identities. `GREENY_FRONTEND_PRODUCT_RESPONSE_EXAMPLE.json`
is a complete schema-compatible product response populated from the observed variant
facts, not a deployed conversation capture. Its price/stock observation is historical;
it is never a frontend fallback or current stock guarantee.

```json
{"session_id":"frontend-demo-01","action":"NAVIGATE_BACK","turn_id":"back-001"}
```

```json
{"session_id":"frontend-demo-01","action":"RESTART","turn_id":"restart-001"}
```

Back returns product detail/questions to the product list, then categories, then
optional name entry, then a new greeting requiring a new speech acknowledgement.
Restart clears conversational history/preferences and starts a new greeting. It
preserves the kiosk-owner binding. It is not deletion of retained audit records.

Equivalent transcript requests for supported intents:

```json
{"session_id":"frontend-demo-01","message":"show bottle","input_mode":"voice"}
```

```json
{"session_id":"frontend-demo-01","message":"show bottle","input_mode":"text"}
```

Both match the button category selection above. At the name prompt, `Noah` typed or
spoken matches SET_NAME; `skip` matches SKIP_NAME. `show all`, `back`, and `start over`
map to the corresponding controls. Speech completion itself is always a structured
acknowledgement. Arbitrary ASR paraphrases are not a guarantee of identical intent.

## 4. Conversation states and screens

| State | Meaning / frontend requirement | Transition |
| --- | --- | --- |
| `GREETING` | Welcome/avatar, input disabled, pending speech token | Matching SPEECH_DONE -> ASK_NAME; Restart issues a new token |
| `ASK_NAME` | Optional name entry and Skip | Name/skip -> ASK_CATEGORY |
| `ASK_CATEGORY` | Dynamic category buttons + Show Everything | Selection -> SHOW_PRODUCTS |
| `SHOW_PRODUCTS` | Actual product cards with canonical IDs | Product -> PRODUCT_DETAIL; Back -> ASK_CATEGORY |
| `PRODUCT_DETAIL` | Product, images, variants, factual status | Question -> PRODUCT_QUESTIONS; Back -> SHOW_PRODUCTS |
| `PRODUCT_QUESTIONS` | Voice/text answer; retain product identity, refresh facts as required | Product selection -> PRODUCT_DETAIL; Back -> SHOW_PRODUCTS |
| `B2B_QUALIFICATION` | Budget/quantity/occasion qualification | Explicit quote -> QUOTE_REQUEST |
| `QUOTE_REQUEST` | Scoped permission prompt, not premature phone collection | Explicit yes/no/revocation; submission remains disabled |
| `HUMAN_HANDOFF` | Display request for store assistance | This action is not proof staff have been notified/arrived |
| `INTENT` | Legacy non-guided intent state | Compatibility state; use returned screen action |
| `QUALIFICATION` | Existing budget/quantity qualification, also used by budget updates in guided sessions | SHOW_PRICE_BANDS or later qualification |
| `PRODUCT_DISCOVERY` | Legacy discovery state | Use returned action |
| `RECOMMENDATION` | Legacy recommendation/detail state | Use returned action |
| `OBJECTION_HANDLING` | Retained legacy enum | No newly verified dedicated guided handler |
| `CONVERSION` | Retained legacy enum | Not proof of an order/checkout |
| `COMPLETED` | Retained legacy terminal/handoff state | Not automatically emitted after consent or a phone number |

Do not infer backend state from a screen name. For example, a greeting uses a
legacy `SHOW_CATEGORIES` action with disabled input, while the normal category
screen is emitted in ASK_CATEGORY. Data/state is authoritative, not client timers.
No real leads, checkout orders or CRM submissions are enabled. Consent prompting
uses `CONFIRM_CONTACT_CONSENT`; `COLLECT_PHONE` follows explicit scoped permission
and includes `submission_enabled: false`. The old guide's immediate COLLECT_PHONE
at quote request and automatic COMPLETED after a phone number were incorrect.

Screen actions flow backend -> frontend: `SHOW_CATEGORIES`, `ASK_NAME`,
`SHOW_PRICE_BANDS`, `SHOW_PRODUCTS`, `SHOW_PRODUCT`, `CONFIRM_CONTACT_CONSENT`,
`COLLECT_PHONE`, `REQUEST_HUMAN`. They are not interchangeable with frontend commands.

The public catalogue read observed categories Bottle, Drinkware, Gift Boxes & Hampers,
Kitchen & Dining, Planter, Storage and Tableware. Actual displayed categories are
computed from fresh eligible products and can change. IDs are normalized UI keys
derived from repository category names (e.g. `kitchen-dining`); they are not claimed
to be website category ObjectIds. `office_desk` / `corporate_b2b` are not assumed
product categories. Do not hardcode the observed list.

## 5. WS v2 envelopes, errors and reconnect

WS input examples omit REST session_id:

```json
{"v":2,"action":"SHOW_ALL","turn_id":"all-002"}
```

```json
{"v":2,"type":"ping"}
```

```json
{"v":2,"type":"cancel","turn_id":"all-002"}
```

A command result uses the existing stream: optional `action`, `token`, optional
`sentence`, then `done`. Even deterministic command replies use this envelope:

```json
{
  "v":2,
  "type":"action",
  "data":{"action":"ASK_NAME","prompt":"What should I call you?","skip_allowed":true,"message":"What should I call you?"},
  "meta":{"turn_id":"greeting-ack-001"}
}
```

```json
{
  "v":2,
  "type":"done",
  "data":"What should I call you?",
  "meta":{
    "session_id":"frontend-demo-01",
    "turn_id":"greeting-ack-001",
    "sales_stage":"ASK_NAME",
    "action":{"action":"ASK_NAME","prompt":"What should I call you?","skip_allowed":true,"message":"What should I call you?"}
  }
}
```

Correlate action/done with the active turn. Stage screen changes until matching
`done`; discard pending actions when canceled, failed or disconnected. Token frames
are speech fragments, not new product evidence. Reject old turn results in the UI.

| Outcome | Frontend behavior |
| --- | --- |
| REST 401/403 or WS identity rejection | Obtain valid authorized credentials; never substitute another kiosk/session |
| REST 422 / WS `validation_error` | Fix command shape; do not retry an unchanged malformed payload |
| REST 409 / WS invalid-stage or duplicate error | Do not advance locally; verify current prompt/token; unknown outcome may require explicit Restart |
| REST 429 | Respect Retry-After; keep input disabled while retrying |
| REST 503 / WS product error | Invalidate old commercial facts; show unavailable/retry, never use a cached card as fresh evidence |
| Cancel/disconnect | Suppress speech/actions; wait for outstanding inference to finish or handle busy response |

Exact product failure response body (REST status 503):

```json
{"detail":{"code":"product_data_unavailable","invalidate_product_facts":true}}
```

WS product failure:

```json
{"v":2,"type":"error","data":"Product information is unavailable.","meta":{"code":"product_data_unavailable","invalidate_product_facts":true}}
```

Other WS errors may have a plain text `data` field and optional metadata. Treat any
error/non-2xx or disconnect as invalidating the freshness of previously displayed
price/stock. Preserve images/name only as unverified context. Do not silently render
cached facts as current when no successful fresh action arrives. Null price/MRP or
stock is unknown, not zero or available. Fresh fields describe an observation;
`stock_checked_at` is provided and repository stock has a 300-second maximum age.
Refresh again before quotation/checkout; there is no stock reservation.

Cancellation reports `provider_cancelled: false` and `output_suppressed: true` for
this synchronous provider path. The inference may finish and update state; do not
claim that provider work or billing stopped. Do not start duplicate turns while busy.

On reconnect, obtain a fresh single-use WS ticket and reuse the session only for the
same kiosk. Keep the last acknowledged screen/speech token locally. WS turn IDs are
deduplicated for 24 hours; recent structured event IDs are also checkpointed (last
128). A consumed greeting token cannot move a later state back to ASK_NAME even
with a new turn ID. There is no session-state/replay endpoint. If state/outcome was
lost, use an explicit new RESTART event or a new session; do not synthesize state
or blindly replay prior commands. A failed WS operation still consumes its turn ID;
a deliberate retry after failure uses a new ID.

## 6. Test and source verification

- **279 passed, 1 skipped, 1 warning in 6.31s.** No failures or errors.
- Guided transport tests: **21 passed**, including real loopback HTTP/WS v2.
- The one skipped test is the opt-in live website schema check; `--run-live` was not used.
- The warning is the existing langchain-community deprecation in app/rag.py.
- `pip check`: no broken requirements. `git diff --check`: clean.
- All 21 inline JSON examples plus the product response artifact passed schema/shape validation.
- Backend source hashes were checked after the full run and match
  `GREENY_BACKEND_SOURCE_MANIFEST.json`. Exact per-module counts are in
  `GREENY_FRONTEND_TEST_RESULTS.json`.
- No commit, push, deployment or production database write was performed.

The network test starts Uvicorn on an ephemeral loopback socket and sends real HTTP
and WebSocket requests with `httpx` and `websockets`. It exercises real handlers,
shared payload validation, LangGraph checkpoint state and signed-ticket issuance.
Only the read-only product repository is mocked, using canonical IDs retrieved from
the actual configured repository. Mock prices are test data, not marketing claims.
It covers all guided states, both name choices, category/all/product/variant selection,
Back, Restart, voice/text/button paths, reconnect, duplicates and service failure.
Additional tests cover malformed JSON shapes, wrong-session speech ACKs and missing
flow endpoints. No frontend or production server was driven by these tests.

Reproduce:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest tests/test_guided_transport_contract.py -q
.\.venv\Scripts\python.exe -m pip check
```

Default tests force local mock/memory settings and skip the external API check.
The repository identity/detail observation was a separate public read, recorded in
`GREENY_FRONTEND_PRODUCT_IDENTITIES.json`. Regenerated `docs/openapi.json` describes
the REST schemas. `GREENY_BACKEND_SOURCE_MANIFEST.json` identifies tested source files.

## 7. Outstanding integration requirements

1. Supply the original frontend guided-flow specification for line-by-line sign-off.
   This report cannot certify agreement with a missing document.
2. Implement the documented UI/TTS acknowledgement, field validation, dynamic
   category rendering, screen-action staging, unknown-data display and reconnect
   behavior in the frontend team's code. No frontend code was changed here.
3. Agree on muted/TTS-failure accessibility behavior. Current progression requires
   a valid completion acknowledgement; timers or synthetic success are not approved.
4. Confirm actual production origin, gateway deployment, token delivery and CORS.
   Loopback success does not establish production connectivity or multi-worker
   MongoDB behavior. Existing persistence/security release gates still apply.
5. Complete live-model/multilingual/ASR and physical-kiosk acceptance. Matching
   validated button/typed/transcript inputs is tested; every possible paraphrase is not.
6. Obtain approved retention/privacy policy for optional names, contact redaction,
   checkpoints and historical data. Name entry is not contact consent. No real lead,
   order, payment or production database write was performed by this audit.
7. Existing sales-state enums do not prove end-to-end checkout or staff notification.
   Those integrations and any frontend expectation of automatic completion require
   an approved business contract.

No commit, push or deployment was performed. Changes remain available for review.
