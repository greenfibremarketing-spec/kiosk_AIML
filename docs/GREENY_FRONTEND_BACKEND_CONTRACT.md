# GREENY FRONTEND-BACKEND API CONTRACT
**Version:** 1.0.0 | **Audited:** 2026-10-08
**Backend base:** `http://localhost:5007`
**WebSocket base:** `ws://localhost:5007`
**Frontend:** Next.js 16 + Electron (app://kiosk protocol)

---

## 1. REST ENDPOINTS

### GET /health
Response 200:
```json
{ "status": "healthy", "service": "Greenie AI Kiosk API", "port": 5007, "llm_provider": "groq", "active_model": "qwen/qwen3.8-27b" }
```

---

### POST /chat — AI conversational reply
Request:
```json
{ "message": "I need 200 Diwali gifts under Rs1500.", "session_id": "kiosk-abc123" }
```
- `message`: string, 1–4000 chars, required
- `session_id`: string, 1–128 chars, `[A-Za-z0-9_.:-]+`, optional (auto-generated if absent)

Response 200:
```json
{ "reply": "For 200 Diwali gifts under 1500 rupees...", "session_id": "kiosk-abc123" }
```
Errors: 400 empty message | 422 validation | 500 LLM/tool error

**GAP:** Frontend never calls this endpoint. `useKiosk.js` uses local `agent.js`.

---

### GET /products — Verified product catalogue
Response 200 (abbreviated):
```json
{
  "products": [{
    "id": "6820dabc1234567890abcdef",
    "sku": "GF:6820dabc1234567890abcdef",
    "website_sku": "GFB-001",
    "name": "Viora Eco Bottle",
    "category": "Drinkware",
    "price": "799.00",
    "mrp": "999.00",
    "currency": "INR",
    "stock": 45,
    "stock_status": "in_stock",
    "in_stock": true,
    "description": "650ml reusable bottle...",
    "source": "greenfibre_api",
    "price_status": "fresh_api",
    "certifications": [],
    "images": ["https://cdn.greenfibre.org/image.jpg"],
    "product_url": "https://greenfibre.org/shop/viora-eco-bottle",
    "variants": [{ "id": "clr1", "sku": "GF:6820d...:clr1", "name": "Forest Green", "images": [], "stock": null }],
    "is_gift_set": false,
    "customization_available": true,
    "customization_types": ["Laser Engraving"],
    "tags": ["drinkware"]
  }],
  "gift_bundles": []
}
```
**GAP:** Frontend uses hardcoded `products.js` with invented data.

---

### POST /session/reset
Request: `{ "session_id": "kiosk-abc123" }`
Response: `{ "status": "ok", "message": "Memory cleared for session: kiosk-abc123" }`

---

### GET /ws/status
Response: `{ "active_connections": 2, "session_ids": ["kiosk-abc123"] }`

---

## 2. WEBSOCKET ENDPOINT

```
ws://localhost:5007/ws/{session_id}
```

**session_id** must match `[A-Za-z0-9_.:-]{1,128}` — server closes with code 1008 otherwise.

### Client → Server
```json
{ "message": "Show me Diwali gift bundles under Rs2000." }
```
Plain text also accepted. Keepalive: `ping` or `{"type":"ping"}`.

### Server → Client
| type | data | meaning |
|---|---|---|
| `"token"` | `"word "` | Streaming word token |
| `"done"` | `"full reply"` | Turn complete (full assembled text) |
| `"error"` | `"message"` | Error, conversation continues |
| `"pong"` | `""` | Keepalive reply |

**GAP:** No WebSocket client in the frontend. Zero streaming integration.

---

## 3. PRODUCT DATA FIELD CONTRACT

Authoritative source is backend `GET /products`. Frontend MUST NOT invent or override.

| Field | Backend | Frontend `products.js` | Status |
|---|---|---|---|
| `id` | MongoDB `_id` 24-char hex | `"viora-bottle"` (slug) | **MISMATCH** |
| `sku` | `GF:{id}` | Not present | **MISSING** |
| `name` | API `name` | Present | OK |
| `price` | String decimal INR | Number literal | **TYPE MISMATCH** |
| `mrp` | `originalPrice` string | Not present | **MISSING** |
| `salePrice` | **Does not exist** | `649` | **INVENTED** |
| `rating` | **Does not exist** | `4.9` | **INVENTED** |
| `reviews` | **Does not exist** | `128` | **INVENTED** |
| `badge` | **Does not exist** | `"Bestseller"` | **INVENTED** |
| `images` | HTTPS CDN URLs | `/images/products/*.jpg` | **LOCAL PATH** |
| `product_url` | `https://greenfibre.org/shop/...` | Not present | **MISSING** |
| `stock_status` | `in_stock/out_of_stock/unknown/inactive` | Not present | **MISSING** |
| `stock` | Integer or null | Not present | **MISSING** |
| `certifications` | `[]` always empty | `"BPA-Free"` in `tags` | **MISLEADING** |
| `customization_available` | Boolean from API | Assumed true | **INFERRED** |
| `tags` | API `tags[]` | Hardcoded | **NOT FROM API** |
| `categories` | `category.slug` | Hardcoded array | **NOT FROM API** |

---

## 4. SESSION AND IDENTITY CONTRACT

| Concern | Current | Required |
|---|---|---|
| Session ID | Auto `web-{uuid}` | Stable `kiosk-{uuid}` in localStorage / IPC |
| Session persistence | Not persisted | Survive page refresh |
| Idle reset | Not integrated | Call `POST /session/reset` on presence exit |
| Auth | None | `X-Kiosk-ID: {device-id}` header (future) |
| CORS | `localhost:3000, localhost:5173` | Add `app://kiosk` |

---

## 5. BACKEND ENVIRONMENT CONTRACT

```env
LLM_PROVIDER=groq|anthropic|mock
GROQ_API_KEY=...
GROQ_MODEL_NAME=llama-3.3-70b-versatile
ANTHROPIC_API_KEY=...
MODEL_NAME=claude-3-5-sonnet-20241022
SESSION_IDLE_TIMEOUT_SECONDS=120
CORS_ORIGINS=http://localhost:3000,http://localhost:5173,app://kiosk
HOST=0.0.0.0
PORT=5007
MAX_MESSAGE_CHARS=4000
PRODUCT_SOURCE=greenfibre
GREENFIBRE_API_BASE_URL=https://api.greenfibre.org/api
GREENFIBRE_API_TOKEN=
GREENFIBRE_API_TIMEOUT_SECONDS=8
GREENFIBRE_CATALOGUE_CACHE_SECONDS=30
GREENFIBRE_API_MAX_PAGES=20
VECTOR_STORE_PATH=data/vector_store
EMBEDDING_MODEL_NAME=sentence-transformers/all-MiniLM-L6-v2
```

## 6. FRONTEND ENVIRONMENT CONTRACT

Frontend reads `config.json` via Electron IPC `kiosk.getConfig`. Add AI fields:

```json
{
  "apiBase": "http://localhost:5007",
  "wsBase": "ws://localhost:5007",
  "enterCm": 180,
  "exitCm": 230,
  "enterDwellMs": 300,
  "exitGraceMs": 5000,
  "focalPx": 580,
  "fps": 6,
  "cameraLabel": "",
  "touchKeepEngagedMs": 30000
}
```

Never embed API keys in browser JavaScript or NEXT_PUBLIC_ vars in production.

---

## 7. TOOL CALL CONTRACT (Backend Internal)

The following LangChain tools are registered in `tools.py`. Frontend can trigger them indirectly via chat.

| Tool | Args | Returns |
|---|---|---|
| `search_products` | `query`, `category`, `max_price`, `occasion` | JSON array of product views (max 5) |
| `get_product_details` | `product_id` | JSON product view |
| `check_stock` | `product_id` | Plain text stock status |
| `get_gift_bundles` | `budget` | JSON array of gift bundle views |
| `prepare_corporate_enquiry` | `product_id`, `quantity`, `occasion`, `customization` | JSON draft (not submitted) |
| `generate_checkout_handoff` | `product_id` | JSON with `url` and `action: "review_product_on_website"` |
