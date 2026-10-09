# Greeny AI Backend-to-Frontend Developer Handoff Specification

**Document Version:** 2.0.0  
**Target Audience:** Frontend Team (Next.js / Electron / Three.js)  
**Backend Framework:** Python 3.13 / FastAPI / LangGraph / PyMongo / LangChain  
**Active Test Status:** **164 PASSED**, 0 failed (100% pass rate)  
**Security Notice:** All database passwords and API credentials remain strictly secured in `.env` and are never logged or exposed.

---

## 1. Executive Summary

This document establishes the verified backend contracts and communication protocols for the Greeny AI Kiosk avatar application.

The Python backend satisfies all frontend requirements natively without needing an interim Node.js gateway:
1. **LangGraph Agent State:** Native integration of immutable `SalesState` into LangGraph's checkpointer.
2. **Deterministic Sales Transitions:** Covers Greeting, Name recognition, Category discovery, Budget filtering, Product recommendations, B2B corporate qualification, customer questions, and Human handoff.
3. **Structured WebSocket v2 Action Events:** Real-time screen actions (`SHOW_CATEGORIES`, `SHOW_PRICE_BANDS`, `SHOW_PRODUCTS`, `SHOW_PRODUCT`, `COLLECT_PHONE`, `REQUEST_HUMAN`) emitted prior to speech streaming.
4. **Authoritative Catalogue Data:** Real Green Fibre SKUs (`GF:...`), variant IDs (`var-...`), prices, MRPs, and image URLs.
5. **Operational Database Persistence:** Automatic database writes to MongoDB collections (`kiosk_sessions`, `messages`, `unanswered_questions`, `customer_consents`, `sales_leads`, `approved_knowledge`) with phone masking and consent enforcement.
6. **Kiosk Authentication & Isolation:** Authenticated boundary checks (`X-Kiosk-Token` / Bearer) and strict cross-kiosk isolation (`CrossKioskAccessError` returning HTTP 403).
7. **Production Fail-Safe:** Fail-safe error responses (HTTP 503 and WebSocket error frames) on MongoDB outage when configured for production persistence—strictly prohibiting silent fallback to temporary in-memory state.
8. **Bounded Token Consumption:** Strict 6-message sliding window history in the agent graph preventing context bloat and token exhaustion over multi-turn dialogues.

---

## 2. REST Endpoint Contracts

Base URL: `http://localhost:5007` (or configured kiosk backend host)

### 2.1 Chat Endpoint: `POST /chat` (Alias: `POST /v1/chat`)

Synchronous conversational interaction with the avatar.

#### Request Headers
| Header | Type | Required | Description |
|---|---|---|---|
| `Content-Type` | string | Yes | `application/json` |
| `X-Kiosk-Token` | string | Optional* | Shared kiosk secret (*required if `KIOSK_AUTH_SECRET` is set) |
| `X-Kiosk-ID` | string | Optional | Hardware identifier of the kiosk (e.g. `kiosk-koramangala-01`) |

#### Request Body
```json
{
  "message": "Tell me more details about the Viora Eco Bottle",
  "session_id": "sess-web-123e4567-e89b",
  "kiosk_id": "kiosk-koramangala-01"
}
```

#### Response (200 OK)
```json
{
  "reply": "The Viora Eco Bottle is crafted from upcycled rice-husk biocomposite and costs ₹649 (MRP ₹799). It comes in Sage Green and Sand Dune variants with 35 units in stock.",
  "session_id": "sess-web-123e4567-e89b",
  "sales_stage": "RECOMMENDATION",
  "action": {
    "action": "SHOW_PRODUCT",
    "sku": "GF:BTL:VIORA",
    "product": {
      "id": "viora-bottle",
      "sku": "GF:BTL:VIORA",
      "name": "Viora Eco Bottle",
      "price": "649",
      "mrp": "799",
      "category": "Drinkware",
      "in_stock": true,
      "stock": 35,
      "description": "Sage-green bottle made from upcycled rice-husk biocomposite.",
      "images": ["https://greenfibre.org/images/viora-sage.jpg"],
      "product_url": "https://greenfibre.org/shop/viora-bottle",
      "variants": [
        {"id": "var-sage", "sku": "GF:BTL:VIORA:SAGE", "name": "Sage Green", "stock": 20},
        {"id": "var-sand", "sku": "GF:BTL:VIORA:SAND", "name": "Sand Dune", "stock": 15}
      ]
    }
  }
}
```

#### Error Responses
- **401 Unauthorized:** Invalid or missing `X-Kiosk-Token` or Bearer token when secret is configured.
- **403 Forbidden:** Cross-kiosk access attempt on another kiosk's session.
- **503 Service Unavailable:** MongoDB persistence outage (fail-safe mode active).

---

### 2.2 Catalogue Endpoint: `GET /products`

Fetches verified catalogue items and gift bundles.

#### Response (200 OK)
```json
{
  "products": [
    {
      "id": "viora-bottle",
      "sku": "GF:BTL:VIORA",
      "name": "Viora Eco Bottle",
      "category": "Drinkware",
      "price": 649,
      "mrp": 799,
      "stock": 35,
      "in_stock": true,
      "variants": [
        {"id": "var-sage", "name": "Sage Green", "stock": 20},
        {"id": "var-sand", "name": "Sand Dune", "stock": 15}
      ]
    }
  ],
  "gift_bundles": [
    {
      "id": "bundle-hydration-duo",
      "name": "Greenie Desk & Hydration Duo",
      "price": 949,
      "mrp": 1298,
      "items": ["Statement Ceramic-Feel Mug", "Viora Eco Bottle"]
    }
  ]
}
```

---

### 2.3 Session Reset: `POST /session/reset` (Alias: `POST /v1/session/reset`)

Clears conversational thread and checkpoint state for the specified session ID.

#### Request Body
```json
{
  "session_id": "sess-web-123e4567-e89b",
  "kiosk_id": "kiosk-koramangala-01"
}
```

#### Response (200 OK)
```json
{
  "status": "ok",
  "message": "Memory cleared for session: sess-web-123e4567-e89b"
}
```

---

### 2.4 Health Check: `GET /health`

#### Response (200 OK)
```json
{
  "status": "healthy",
  "service": "Greenie AI Kiosk API",
  "port": 5007,
  "llm_provider": "groq",
  "active_model": "llama-3.3-70b-versatile",
  "checkpointer_backend": "mongodb",
  "database": {
    "status": "healthy",
    "target_db": "greeny_ai",
    "collections": ["kiosk_sessions", "messages", "customer_consents", "sales_leads", "unanswered_questions", "approved_knowledge"],
    "retention_policy": "90_days"
  }
}
```

---

## 3. Real-Time WebSocket Streaming Protocol v2

Endpoint: `ws://localhost:5007/ws/{session_id}?v=2`

### 3.1 Authentication
If `KIOSK_AUTH_SECRET` is configured on the backend, the client must supply the secret via query parameter or header:
- URL query parameter: `ws://localhost:5007/ws/{session_id}?v=2&token=<KIOSK_AUTH_SECRET>&kiosk_id=<KIOSK_ID>`
- Or HTTP header on upgrade: `X-Kiosk-Token: <KIOSK_AUTH_SECRET>`

### 3.2 Client → Server Frames

1. **User Message (JSON):**
   ```json
   {
     "v": 2,
     "message": "Show me drinkware bottles",
     "kiosk_id": "kiosk-koramangala-01"
   }
   ```
2. **Interrupt / Turn Cancellation:**
   ```json
   {
     "type": "cancel"
   }
   ```
3. **Heartbeat Keepalive:**
   ```json
   {
     "type": "ping"
   }
   ```
   *(Plain string `"ping"` is also supported and answered immediately with `"pong"`).*

### 3.3 Server → Client Frames

During a conversational turn, the server emits events in the following strict order:

```
+-------------------------------------------------------------------+
| 1. {"v": 2, "type": "action", "data": { ... }}                     |
|    (Emitted immediately if turn triggers UI screen change)        |
+-------------------------------------------------------------------+
                                  |
                                  v
+-------------------------------------------------------------------+
| 2. {"v": 2, "type": "token", "data": "Here "}                     |
|    {"v": 2, "type": "token", "data": "is "}                       |
|    {"v": 2, "type": "token", "data": "our "}                      |
|    {"v": 2, "type": "token", "data": "bottle."}                   |
|    (Streaming word tokens for UI text animation)                  |
+-------------------------------------------------------------------+
                                  |
                                  v
+-------------------------------------------------------------------+
| 3. {"v": 2, "type": "sentence", "data": "Here is our bottle."}    |
|    (Full punctuated sentence for TTS engine synthesis)            |
+-------------------------------------------------------------------+
                                  |
                                  v
+-------------------------------------------------------------------+
| 4. {"v": 2, "type": "done",                                       |
|     "data": "Here is our bottle.",                                |
|     "meta": {"session_id": "...", "sales_stage": "...", ...}}     |
|    (Signals completion of turn)                                   |
+-------------------------------------------------------------------+
```

---

## 4. Structured UI Actions Reference

When the avatar decides on a visual transition, the `action` frame contains one of the following 6 actions.

### 4.1 Action: `SHOW_CATEGORIES`
Triggered during Greeting or Category inquiries.

```json
{
  "v": 2,
  "type": "action",
  "data": {
    "action": "SHOW_CATEGORIES",
    "categories": [
      {"id": "drinkware", "name": "Drinkware & Bottles", "icon": "bottle"},
      {"id": "kitchen_dining", "name": "Kitchen & Dining", "icon": "utensils"},
      {"id": "office_desk", "name": "Office & Desk", "icon": "briefcase"},
      {"id": "gift_bundles", "name": "Gift Sets & Bundles", "icon": "gift"},
      {"id": "corporate_b2b", "name": "Corporate & Bulk Gifting", "icon": "building"}
    ]
  }
}
```

### 4.2 Action: `SHOW_PRICE_BANDS`
Triggered when user inquires about price ranges or specifies a budget.

```json
{
  "v": 2,
  "type": "action",
  "data": {
    "action": "SHOW_PRICE_BANDS",
    "bands": [
      {"id": "under_500", "label": "Under ₹500", "min": 0, "max": 500},
      {"id": "500_1000", "label": "₹500 - ₹1,000", "min": 500, "max": 1000},
      {"id": "1000_1500", "label": "₹1,000 - ₹1,500", "min": 1000, "max": 1500},
      {"id": "above_1500", "label": "Above ₹1,500", "min": 1500, "max": null}
    ],
    "selected_budget": 800.0
  }
}
```

### 4.3 Action: `SHOW_PRODUCTS`
Triggered when displaying product recommendations or catalogue discovery cards.

```json
{
  "v": 2,
  "type": "action",
  "data": {
    "action": "SHOW_PRODUCTS",
    "count": 2,
    "products": [
      {
        "id": "viora-bottle",
        "sku": "GF:BTL:VIORA",
        "name": "Viora Eco Bottle",
        "price": "649",
        "mrp": "799",
        "category": "Drinkware",
        "in_stock": true,
        "stock": 35,
        "image": "https://greenfibre.org/images/viora-sage.jpg",
        "variants_count": 2
      },
      {
        "id": "statement-mug",
        "sku": "GF:MUG:STAT",
        "name": "Statement Ceramic-Feel Mug",
        "price": "399",
        "mrp": "499",
        "category": "Drinkware",
        "in_stock": true,
        "stock": 50,
        "image": "https://greenfibre.org/images/mug-charcoal.jpg",
        "variants_count": 2
      }
    ]
  }
}
```

### 4.4 Action: `SHOW_PRODUCT`
Triggered when user asks for specific product specifications, prices, or variants.

```json
{
  "v": 2,
  "type": "action",
  "data": {
    "action": "SHOW_PRODUCT",
    "sku": "GF:BTL:VIORA",
    "product": {
      "id": "viora-bottle",
      "sku": "GF:BTL:VIORA",
      "name": "Viora Eco Bottle",
      "price": "649",
      "mrp": "799",
      "category": "Drinkware",
      "in_stock": true,
      "stock": 35,
      "description": "Sage-green bottle made from upcycled rice-husk biocomposite.",
      "images": ["https://greenfibre.org/images/viora-sage.jpg"],
      "product_url": "https://greenfibre.org/shop/viora-bottle",
      "variants": [
        {
          "id": "var-sage",
          "sku": "GF:BTL:VIORA:SAGE",
          "name": "Sage Green",
          "stock": 20
        },
        {
          "id": "var-sand",
          "sku": "GF:BTL:VIORA:SAND",
          "name": "Sand Dune",
          "stock": 15
        }
      ]
    }
  }
}
```

### 4.5 Action: `COLLECT_PHONE`
Triggered for B2B corporate qualification, volume discounts, or custom logo quote generation.

```json
{
  "v": 2,
  "type": "action",
  "data": {
    "action": "COLLECT_PHONE",
    "reason": "corporate_quote",
    "lead_type": "b2b",
    "quantity": 100
  }
}
```

### 4.6 Action: `REQUEST_HUMAN`
Triggered when customer requests human staff assistance or when an unresolvable issue requires physical store associate escalation.

```json
{
  "v": 2,
  "type": "action",
  "data": {
    "action": "REQUEST_HUMAN",
    "reason": "customer_requested",
    "message": "Store associate requested by customer."
  }
}
```

---

## 5. Operational MongoDB Architecture

The operational database is `greeny_ai`. All operational records are automatically stored with zero frontend overhead:

| Collection | Schema / Purpose | Privacy & Guardrails |
|---|---|---|
| `kiosk_sessions` | Tracks active kiosk sessions, turn counts, current sales stage, customer name. | Updated on each turn via `$set` and `$inc`. |
| `messages` | Historical conversation dialogue (human queries & AI replies with tool outputs). | Auto-inserted with UTC timestamp and session ID. |
| `unanswered_questions` | Out-of-catalogue or unresolvable inquiries requiring human review. | Recorded with reason (e.g. "Out of catalogue domain"). |
| `customer_consents` | Audit trail of customer consent grants for follow-up quotes. | Phone numbers are masked: `+919****3210`. |
| `sales_leads` | Qualified B2B corporate leads with quantities and custom requirements. | Strictly refuses unconsented phone numbers (`ValueError`). |
| `approved_knowledge` | Verified facts and compliance articles for RAG. | Indexed by `doc_id` with SHA-256 integrity hash. |

### Outage Fail-Safe Behavior
In production (`CHECKPOINTER_BACKEND=mongodb`):
- Any MongoDB network loss or database outage raises `DatabaseConnectionError`.
- REST endpoints return **HTTP 503**: `{"detail": "Database persistence unavailable. Outage fail-safe active."}`.
- WebSocket streaming returns an error frame: `{"v": 2, "type": "error", "data": "Database persistence unavailable. Outage fail-safe active."}`.
- **Silent degradation to ephemeral memory is strictly disabled in production.**

---

## 6. Multi-Turn Dialogue & Guardrails Audit

A 20-turn dialogue audit was executed and verified (`tests/test_twenty_turn_conversation.py`).

### Dialogue Progression Summary
1. **Turn 1 (Greeting):** Avatar greets warmly; emits `SHOW_CATEGORIES`.
2. **Turn 2 (Name):** Customer shares name ("Aarav Sharma"); avatar registers name without state mutation.
3. **Turn 3 (Categories):** Customer asks for available categories; emits `SHOW_CATEGORIES`.
4. **Turn 4 (Discovery):** Customer asks about bottles; emits `SHOW_PRODUCTS`.
5. **Turn 5 (Budget):** Customer specifies budget "under 700 rupees"; emits `SHOW_PRICE_BANDS` with selected budget 700.0.
6. **Turn 6 (Detail):** Customer asks about Viora Eco Bottle; emits `SHOW_PRODUCT` with real variants (Sage Green, Sand Dune).
7. **Turn 7 (Materials):** Customer asks about material; RAG grounds answer on rice-husk biocomposite.
8. **Turn 8 (Care):** Customer asks about dishwashability; RAG grounds answer on dishwasher safety.
9. **Turn 9 (Detail):** Customer asks about Statement Mug; emits `SHOW_PRODUCT` (Charcoal, Ivory).
10. **Turn 10 (Safety):** Customer asks about microwave; RAG confirms microwave-reheat safety without melamine.
11. **Turn 11 (Commute):** Customer asks about travel tumblers; emits `SHOW_PRODUCTS` (Origin Tumbler).
12. **Turn 12 (Kitchen):** Customer asks about salad bowls; emits `SHOW_PRODUCT` (Flora Bowl).
13. **Turn 13 (Bundles):** Customer asks for gift sets; tool returns verified bundle (Desk Hydration Duo ₹949).
14. **Turn 14 (Shipping):** Policy RAG answers standard shipping timeline.
15. **Turn 15 (Returns):** Policy RAG explains 7-day hassle-free return policy.
16. **Turn 16 (B2B Volume):** Customer requests 120 units for Diwali gifting; emits `COLLECT_PHONE` (lead_type: b2b, quantity: 120).
17. **Turn 17 (Customization):** Customer asks about logo engraving; notes "Logo Engraving" in customization requirements.
18. **Turn 18 (Out of Scope):** Customer asks about laptops/phones; logs question in `unanswered_questions`.
19. **Turn 19 (Handoff):** Customer requests human store manager; transitions to COMPLETED, marks `escalation_required=True`, emits `REQUEST_HUMAN`.
20. **Turn 20 (Closing):** Customer says thank you; avatar provides polite, closing farewell.

### Guardrail Verification Results
- **Spoken Text Formatting:** 0 markdown characters, 0 bullet points, 0 emojis, 1 to 3 spoken sentences per turn.
- **Zero Invented Prices:** All mentioned numbers (₹649, ₹799, ₹399, ₹499, ₹749, ₹899, ₹549, ₹699, ₹949, ₹1298) strictly verified against catalogue data.
- **Bounded Tokens:** Sliding window keeps at most 6 recent messages in the active LLM context, ensuring uniform response latency and preventing Groq token exhaustion.

---

## 7. Test Evidence and Verification Suite

Complete automated test run executed on Windows 11 with Python 3.13:

```text
============================= test session starts =============================
platform win32 -- Python 3.13.3, pytest-9.1.1, pluggy-1.6.0
rootdir: C:\Users\Shivansh Dubey\OneDrive\Desktop\Kiosk_AIML
collected 164 items

tests/test_actions_and_identity.py ............                          [  7%]
tests/test_api_reliability.py .........                                 [ 12%]
tests/test_brain_reliability.py ........                                [ 17%]
tests/test_frontend_contract_audit.py ......                             [ 21%]
tests/test_greenfibre_repository.py ..................................  [ 42%]
tests/test_mongo_persistence.py ...........                              [ 48%]
tests/test_product_chunks.py ....                                        [ 51%]
tests/test_product_tools.py ........................                     [ 65%]
tests/test_sales_engine.py ...........                                   [ 72%]
tests/test_sales_state.py ...................................            [ 93%]
tests/test_session_reset.py ..                                           [ 95%]
tests/test_twenty_turn_conversation.py .                                 [ 95%]
tests/test_ws_protocol_v2.py .....                                       [100%]

======================= 164 passed, 1 warning in 16.67s =======================
```

---

## 8. Frontend Integration Checklist

| Item | Frontend Status | Backend Readiness | Notes |
|---|---|---|---|
| Connect to WebSocket v2 | Pending UI Hook | **READY** (`ws://host:5007/ws/{id}?v=2`) | Pass `kiosk_id` and optional `token`. |
| Render `action` events | Pending Component | **READY** (`type: "action"`) | Listen for `action.data.action` to update 3D UI / cards. |
| Render `SHOW_CATEGORIES` | Pending Carousel | **READY** (5 categories) | Icons provided: `bottle`, `utensils`, `briefcase`, etc. |
| Render `SHOW_PRICE_BANDS` | Pending Chips | **READY** (4 bands) | Includes `selected_budget` float if detected. |
| Render `SHOW_PRODUCTS` | Pending Carousel | **READY** (cards with SKU & MRP) | Use `product.image` and `product.sku`. |
| Render `SHOW_PRODUCT` | Pending Modal / View | **READY** (variants & stock) | Render color variant swatches from `product.variants`. |
| Render `COLLECT_PHONE` | Pending Modal | **READY** (B2B phone prompt) | Post consent to `/customer/consent` before lead storage. |
| Render `REQUEST_HUMAN` | Pending Banner / Call | **READY** (Staff notification) | Displays "Associate on the way". |
| Heartbeat Ping | Pending Timer | **READY** (`{"type": "ping"}`) | Server responds immediately with `pong`. |
| Turn Cancellation | Pending Speech Stop | **READY** (`{"type": "cancel"}`) | Halts speech synthesis and token streaming. |
