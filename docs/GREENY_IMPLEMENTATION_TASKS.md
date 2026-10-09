# GREENY IMPLEMENTATION TASKS
**Priority order for smallest viable E2E integration**
**Rule:** Each task must not push, merge, deploy, or modify production data. All changes are local dev only.

---

## SPRINT 0 — Smallest Viable E2E Integration
Goal: One frontend message reaches LangGraph; one AI reply appears in the kiosk caption.

---

### TASK-01 — Add app://kiosk to backend CORS (5 min)
**File:** `app/config.py`, `.env.example`
**Owner:** Backend
**Priority:** P0
**Estimated effort:** 5 minutes

Change `.env.example`:
```
CORS_ORIGINS=http://localhost:3000,http://localhost:5173,app://kiosk
```

No code change needed — `cors_origin_list` property already parses comma-separated string.

**Test acceptance:** `curl -H "Origin: app://kiosk" http://localhost:5007/health` returns 200 with `Access-Control-Allow-Origin: app://kiosk`.

---

### TASK-02 — Create frontend WebSocket/REST client (2 hours)
**File:** `src/lib/backend.js` (new file)
**Owner:** Frontend
**Priority:** P0
**Estimated effort:** 2 hours

```javascript
// src/lib/backend.js
const API_BASE = typeof window !== "undefined" && window.kiosk?.config?.apiBase
  ? window.kiosk.config.apiBase
  : "http://localhost:5007";
const WS_BASE = API_BASE.replace(/^http/, "ws");

let ws = null;
let sessionId = null;
let onTokenCallback = null;
let onDoneCallback = null;
let onErrorCallback = null;

export function getSessionId() {
  if (!sessionId) {
    sessionId = localStorage.getItem("greenie_session_id");
    if (!sessionId) {
      sessionId = "kiosk-" + crypto.randomUUID().replace(/-/g, "");
      localStorage.setItem("greenie_session_id", sessionId);
    }
  }
  return sessionId;
}

export function connectBackend({ onToken, onDone, onError }) {
  onTokenCallback = onToken;
  onDoneCallback = onDone;
  onErrorCallback = onError;
  const sid = getSessionId();
  ws = new WebSocket(`${WS_BASE}/ws/${sid}`);
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "token") onTokenCallback?.(msg.data);
      if (msg.type === "done") onDoneCallback?.(msg.data);
      if (msg.type === "error") onErrorCallback?.(msg.data);
    } catch (_) {}
  };
  ws.onclose = () => setTimeout(() => connectBackend({ onToken, onDone, onError }), 2000);
  ws.onerror = () => ws.close();
}

export function sendMessage(text) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    // REST fallback
    fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, session_id: getSessionId() }),
    })
      .then((r) => r.json())
      .then((d) => onDoneCallback?.(d.reply))
      .catch(() => onErrorCallback?.("I am having trouble connecting. Please ask a store associate."));
    return;
  }
  ws.send(JSON.stringify({ message: text }));
}

export async function resetSession() {
  const sid = getSessionId();
  await fetch(`${API_BASE}/session/reset`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sid }),
  }).catch(() => {});
}

export async function fetchProducts() {
  const r = await fetch(`${API_BASE}/products`);
  if (!r.ok) throw new Error("Products unavailable");
  return r.json();
}
```

**Test acceptance:** WebSocket connects to `ws://localhost:5007/ws/kiosk-{uuid}`; sending a message returns token stream ending with "done".

---

### TASK-03 — Wire backend into useKiosk.js (2 hours)
**File:** `src/hooks/useKiosk.js`
**Owner:** Frontend
**Priority:** P0
**Estimated effort:** 2 hours

Key changes:
```javascript
import { connectBackend, sendMessage, resetSession, fetchProducts } from "@/lib/backend";

// On mount: connect WS and load products from API
useEffect(() => {
  connectBackend({
    onToken: (token) => setCaption(prev => prev + token),
    onDone: (full) => { setCaption(full); speak(full); },
    onError: (err) => setCaption("I am having trouble. Please ask a store associate."),
  });
  fetchProducts()
    .then(data => setProducts(data.products))
    .catch(() => {}); // Keep local fallback products if fetch fails
}, []);

// Replace local reply() call with backend:
const handleMessage = useCallback((text) => {
  setCaption(""); // Clear for streaming
  sendMessage(text);
}, []);

// On idle reset: also clear backend session
const reset = useCallback(() => {
  resetSession();
  setCaption(GREETING);
  // ... other reset state
}, []);
```

**Test acceptance:** Customer message routes to backend; caption streams in; final text matches backend reply.

---

### TASK-04 — Connect useSpeech to backend reply (1 hour)
**File:** `src/hooks/useSpeech.js`, `src/hooks/useKiosk.js`
**Owner:** Frontend
**Priority:** P1
**Estimated effort:** 1 hour

Add `speak(text)` export to `useSpeech`:
```javascript
const speak = useCallback((text) => {
  if (!text || isSpeakingRef.current) return;
  stopContinuousListening();
  const utt = new SpeechSynthesisUtterance(text);
  utt.lang = "en-IN"; // or "hi-IN" if Hindi detected
  utt.onstart = () => { setSpeaking(true); isSpeakingRef.current = true; };
  utt.onend = () => { setSpeaking(false); isSpeakingRef.current = false; startContinuousListening(); };
  speechSynthesis.cancel();
  speechSynthesis.speak(utt);
}, [startContinuousListening, stopContinuousListening]);
```

Pass `speak` to `useKiosk` and call it from `onDone` callback.

**Test acceptance:** Avatar speaks reply once. Listening resumes after speech ends. New customer input during speech cancels TTS.

---

### TASK-05 — Replace product data with API data (3 hours)
**File:** `src/data/products.js`, `src/hooks/useKiosk.js`, product card components
**Owner:** Frontend
**Priority:** P0
**Estimated effort:** 3 hours

- Fetch `GET /products` on mount and store in React state.
- Remove invented fields: `rating`, `reviews`, `badge`, `salePrice`.
- Replace `images[0]` local path with `images[0]` HTTPS URL from API.
- Show `price` as `"₹" + product.price` (string, not number).
- Show `product_url` as a "View on Website" link or QR code.
- Keep `src/data/products.js` as a hardcoded offline fallback ONLY labeled as "Demo Mode".

**Test acceptance:** Product cards show real names, real prices (or "Price on request"), real images. No invented ratings or badges appear.

---

### TASK-06 — Add config.json apiBase to Electron IPC (30 min)
**File:** `main.js`, `preload.js`, `src/lib/presence/usePresenceDetection.js`
**Owner:** Frontend
**Priority:** P1
**Estimated effort:** 30 minutes

Add to `config.json`:
```json
{ "apiBase": "http://localhost:5007", "wsBase": "ws://localhost:5007" }
```

Expose via preload:
```javascript
contextBridge.exposeInMainWorld("kiosk", {
  getConfig: () => ipcRenderer.invoke("get-config"),
  // already present
});
```

Read in `backend.js`:
```javascript
const cfg = await window.kiosk?.getConfig?.() ?? {};
const API_BASE = cfg.apiBase ?? "http://localhost:5007";
```

**Test acceptance:** `window.kiosk.getConfig()` returns object with `apiBase`.

---

## SPRINT 1 — Product Data and Avatar Polish

### TASK-07 — Normalize product schema in frontend (2 hours)
**File:** `src/components/products/ProductCard.jsx` (or equivalent)
**Owner:** Frontend
**Priority:** P1
**Estimated effort:** 2 hours

- Accept `product` prop with backend schema fields.
- Display `stock_status` as a badge: "In Stock" / "Out of Stock" / "Check in store".
- Display `customization_available` as a label.
- Add "View on GreenFibre.org" button using `product_url`.

---

### TASK-08 — Implement checkout handoff rendering (1 hour)
**File:** `src/hooks/useKiosk.js`, product card or sidebar component
**Owner:** Frontend
**Priority:** P2
**Estimated effort:** 1 hour

When backend reply mentions a product URL (parsed from tool result embedded in `"done"` message or via a new `metadata` field):
- Display a QR code of `product_url`.
- Show "Scan to view on GreenFibre.org" caption.
- No order, no cart, no payment.

---

### TASK-09 — Add reconnection and offline fallback (1 hour)
**File:** `src/lib/backend.js`
**Owner:** Frontend
**Priority:** P2
**Estimated effort:** 1 hour

- Exponential backoff: 500ms → 2s → 5s, max 3 attempts.
- After 3 failures: set `isOffline = true`; display "Demo mode" indicator.
- In demo mode, route to local `agent.js` with explicit label: "This is a demo response."
- Timeout: if no `"done"` after 15 seconds, show "Unable to reach Greeny. Ask a store associate."

---

## SPRINT 2 — Security and Session Hardening

### TASK-10 — Add app://kiosk CSP in Electron (30 min)
**File:** `main.js`
**Owner:** Frontend
**Priority:** P1
**Estimated effort:** 30 minutes

Add session content security policy:
```javascript
session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
  callback({
    responseHeaders: {
      ...details.responseHeaders,
      "Content-Security-Policy": [
        "default-src 'self' app://kiosk; connect-src 'self' http://localhost:5007 ws://localhost:5007 https://api.greenfibre.org https://greenfibre.org; img-src 'self' https: data:; script-src 'self' 'unsafe-inline';"
      ]
    }
  });
});
```

---

### TASK-11 — Backend rate limiting (2 hours)
**File:** `app/server.py`
**Owner:** Backend
**Priority:** P2
**Estimated effort:** 2 hours

- Per-session: max 20 messages/min (track in `session_manager._last_active` with a counter).
- Global: max 100 concurrent WS connections (check `len(ws_manager.active)`).
- Return `{"type":"error","data":"Too many requests."}` on WS; 429 on REST.

---

### TASK-12 — Add emotion signal guard (1 hour)
**File:** `src/hooks/useKiosk.js`
**Owner:** Frontend
**Priority:** P2
**Estimated effort:** 1 hour

Strip `mood`, `smile`, `sad`, `confused`, `surprised` from any backend-bound payload. Only pass `present` (boolean) and `attention` ("screen"/"away") as optional context in a `signals` metadata field — never in the `message` string itself.

---

## SPRINT 3 — B2B Gifting and Proactive Engagement

### TASK-13 — B2B sales state tracking (3 hours)
**File:** `src/hooks/useKiosk.js`, new `src/lib/salesState.js`
**Owner:** Frontend
**Priority:** P2
**Estimated effort:** 3 hours

Track from LangGraph replies (parse `"done"` text for keywords or add structured `metadata` to WS `"done"` message):
```javascript
const [salesState, setSalesState] = useState({
  quantity: null, budget: null, occasion: null,
  selectedProductSku: null, draftReady: false,
});
```

- Budget: detect "under Rs X" or "X rupees" in the customer message and echo to state.
- Quantity: detect "N gifts" pattern.
- This state is display-only; truth of record is in LangGraph session memory.

---

### TASK-14 — Proactive engagement controller (2 hours)
**File:** `src/hooks/useKiosk.js`
**Owner:** Frontend
**Priority:** P1
**Estimated effort:** 2 hours

Proactive message rules:
- Attract mode (IDLE): local hardcoded strings, no backend call.
- Re-engage after 9s silence (ENGAGED): call backend with `"[SYSTEM] Customer has been quiet for 9 seconds. Ask one helpful question."`.
- Product scroll: when user scrolls to a new product, call backend with `"[SYSTEM] Customer is viewing {product.name}. Offer one helpful fact."`.
- Rate limit: max 1 proactive backend call per 30 seconds.
- Never interrupt speech in progress.

---

### TASK-15 — Backend: add structured metadata to WS "done" message (1 hour)
**File:** `app/server.py`, `app/brain.py`
**Owner:** Backend
**Priority:** P2
**Estimated effort:** 1 hour

Extend WebSocket `"done"` message to include optional metadata:
```json
{
  "type": "done",
  "data": "For 200 Diwali gifts...",
  "metadata": {
    "product_urls": ["https://greenfibre.org/shop/festive-gift-set"],
    "tool_used": "get_gift_bundles",
    "session_id": "kiosk-abc123"
  }
}
```

This allows frontend to display product URLs and QR codes without text parsing.

---

## TEST ACCEPTANCE CRITERIA — Smoke Test After TASK-01 to TASK-04

Run after each task is merged to a local branch:

1. Start backend: `python main.py` (with `LLM_PROVIDER=mock` for offline test)
2. Start frontend: `npm run dev` (in Electron dev mode)
3. Open kiosk window
4. Verify: `GET http://localhost:5007/health` returns 200 in browser console
5. Say or type: "I need 200 Diwali gifts under 1500 rupees"
6. Verify:
   - Caption streams in word by word
   - Avatar speaks the complete reply
   - `session_id` is present in localStorage
   - Backend log shows `WS connected: session=kiosk-{uuid}`
   - Backend log shows `event: http_request` entries
7. Say: "What about mugs specifically?"
   - Verify: backend remembers previous gift context (multi-turn)
8. Close and reopen frontend
   - Verify: same `session_id` recovered from localStorage

**MOCK model note:** The mock model (LLM_PROVIDER=mock) will trigger `get_gift_bundles(budget=1500.0)` and return a plausible response. Product data will be from the live GreenFibre API (PRODUCT_SOURCE=greenfibre) or from local JSON (PRODUCT_SOURCE=json with LLM_PROVIDER=mock).

---

## TASK SUMMARY TABLE

| ID | Title | File(s) | Priority | Owner | Effort | Sprint |
|---|---|---|---|---|---|---|
| TASK-01 | CORS for app://kiosk | config.py, .env.example | P0 | Backend | 5m | 0 |
| TASK-02 | Create backend.js WS client | src/lib/backend.js | P0 | Frontend | 2h | 0 |
| TASK-03 | Wire backend into useKiosk | useKiosk.js | P0 | Frontend | 2h | 0 |
| TASK-04 | Connect useSpeech to reply | useSpeech.js | P1 | Frontend | 1h | 0 |
| TASK-05 | Replace product data | products.js, cards | P0 | Frontend | 3h | 0 |
| TASK-06 | config.json apiBase IPC | main.js, preload.js | P1 | Frontend | 30m | 0 |
| TASK-07 | Normalize product schema | ProductCard | P1 | Frontend | 2h | 1 |
| TASK-08 | Checkout handoff QR | useKiosk, sidebar | P2 | Frontend | 1h | 1 |
| TASK-09 | Reconnection + offline fallback | backend.js | P2 | Frontend | 1h | 1 |
| TASK-10 | Electron CSP | main.js | P1 | Frontend | 30m | 2 |
| TASK-11 | Backend rate limiting | server.py | P2 | Backend | 2h | 2 |
| TASK-12 | Emotion signal guard | useKiosk.js | P2 | Frontend | 1h | 2 |
| TASK-13 | B2B sales state tracking | useKiosk, salesState.js | P2 | Frontend | 3h | 3 |
| TASK-14 | Proactive engagement controller | useKiosk.js | P1 | Frontend | 2h | 3 |
| TASK-15 | WS "done" metadata field | server.py, brain.py | P2 | Backend | 1h | 3 |

**Sprint 0 estimated total:** ~8.5 hours of implementation work.
