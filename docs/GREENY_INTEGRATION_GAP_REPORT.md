# GREENY INTEGRATION GAP REPORT
**Audited:** 2026-10-08 | **Backend branch:** `feature/greeny-reliability-fixes` | **Frontend branch:** `main`

---

## CRITICAL GAPS (P0 — Blocker)

### GAP-01: Frontend has NO AI backend integration
**File:** `src/lib/agent.js`, `src/hooks/useKiosk.js`
**Owner:** Frontend
**Root cause:** `useKiosk.js` imports `reply()` from `agent.js`, which is a pure local keyword-matching function. There is no `fetch`, `WebSocket`, or any HTTP call to `localhost:5007` anywhere in the frontend codebase.
**Impact:** Every customer interaction gets a canned, rule-based response. LangGraph is never reached.
**Required changes:**
- Create `src/lib/backend.js` with `sendMessage(sessionId, message)` using WebSocket (`ws://localhost:5007/ws/{sessionId}`) or `POST /chat` fallback.
- Modify `useKiosk.js` to call the backend instead of `reply()`.
- Show a "connecting…" or "offline demo" label when the backend is unreachable.
**Acceptance criteria:** Customer message "I need 200 Diwali gifts under Rs1500" triggers a network request to localhost:5007; LangGraph responds; reply appears in caption and avatar speech.

---

### GAP-02: Product IDs are incompatible between frontend and backend
**File:** `src/data/products.js`, `app/tools.py`, `app/greenfibre_repository.py`
**Owner:** Frontend
**Root cause:** Frontend uses human-readable slugs like `"viora-bottle"`. Backend uses MongoDB 24-char hex IDs wrapped as `GF:6820dabc...`. When the frontend passes a product slug to the backend tool (`get_product_details`, `check_stock`), the backend returns `null` because no product matches the slug.
**Required changes:**
- Remove hardcoded `products.js`. Replace with a dynamic fetch from `GET /products` on startup.
- Store `sku` (e.g. `GF:6820d...`) as the canonical product identifier throughout the frontend.
- Update `selectProduct()` in `useKiosk.js` to include the `sku` in any message sent to the AI.
**Acceptance criteria:** `check_stock("GF:6820dabc...")` returns a valid stock status; the avatar reads it aloud; the frontend displays the correct stock-aware badge.

---

### GAP-03: Frontend displays invented product data
**File:** `src/data/products.js`, `src/components/products/`
**Owner:** Frontend
**Root cause:** `products.js` contains hardcoded `rating: 4.9`, `reviews: 128`, `badge: "Bestseller"`, `salePrice: 649`, and local image paths (`/images/products/viora-bottle.jpg`). None of these exist in the backend API. Images will 404 in production. Ratings and reviews are fabricated social proof.
**Impact:** Legal/compliance risk; customer trust violation; images break in production.
**Required changes:**
- Fetch products from `GET /products` on startup; persist in React state.
- Remove `rating`, `reviews`, `badge`, `salePrice` fields from all display components.
- Replace local image paths with `images[]` HTTPS URLs from the API.
- Add `product_url` link / QR code from `product_url` field.
**Acceptance criteria:** No invented fields displayed. Product images load from CDN. Products without prices show "Price on request" not a number.

---

### GAP-04: Session ID is not stable or persisted
**File:** `src/hooks/useKiosk.js`, `app/server.py`
**Owner:** Frontend + Backend
**Root cause:** Backend auto-generates `web-{uuid}` when `session_id` is omitted. Frontend never generates, stores, or sends a `session_id`. Every request or WebSocket connection starts a new conversation with no memory.
**Impact:** Multi-turn conversation is impossible. B2B gifting qualification state is lost.
**Required changes:**
- Frontend: generate `kiosk-{uuid}` on first load; persist in `localStorage`.
- Frontend: send `session_id` in every request body and WS URL.
- Frontend: call `POST /session/reset` when presence exits (idle reset).
- Backend: add `app://kiosk` to CORS_ORIGINS.
**Acceptance criteria:** Two consecutive messages share conversation history. Backend returns matching `session_id`. Idle reset clears LangGraph thread.

---

## HIGH PRIORITY GAPS (P1 — Required for live demo)

### GAP-05: No WebSocket streaming client in frontend
**File:** `src/hooks/useKiosk.js` (new file needed: `src/lib/backend.js`)
**Owner:** Frontend
**Root cause:** The backend streaming endpoint (`ws://localhost:5007/ws/{sessionId}`) delivers tokens word-by-word for low-latency TTS. The frontend has no WebSocket client. `useSpeech.js` cannot receive streaming input.
**Required changes:**
- Build `useBackend` hook: maintain a `WebSocket`, reconnect on disconnect, queue outbound messages, yield tokens to `useSpeech`.
- On `"token"` message: append to caption text progressively.
- On `"done"` message: pass full reply to TTS engine.
- On `"error"` message: show fallback message; do NOT speak backend internal errors.
**Acceptance criteria:** Avatar begins speaking within 1 second of first token; full reply caption matches spoken text.

---

### GAP-06: avatar.js / useSpeech not connected to backend reply
**File:** `src/hooks/useSpeech.js`, `src/hooks/useKiosk.js`
**Owner:** Frontend
**Root cause:** `useSpeech.js` has `setSpeaking(true)` / `setSpeaking(false)` state but is never called with the backend reply text. `say()` in `useKiosk.js` only sets `caption`; it does not call any TTS `speak()` function.
**Required changes:**
- Expose `speak(text)` from `useSpeech`.
- Call `speak(reply)` after receiving `"done"` from backend.
- Pause `startContinuousListening()` during `speaking === true`.
- Resume listening after `speaking === false`.
- Allow speech interruption: if new customer speech starts, call `speechSynthesis.cancel()`.
**Acceptance criteria:** Avatar speaks exactly once per turn. Listening resumes after speech ends. Customer can interrupt.

---

### GAP-07: Electron CORS origin not whitelisted on backend
**File:** `app/config.py`, `.env.example`
**Owner:** Backend
**Root cause:** Backend `CORS_ORIGINS` defaults to `http://localhost:3000,http://localhost:5173`. Electron loads pages via the custom `app://kiosk` protocol, which has a different origin. Fetch and WebSocket calls from Electron will be blocked by CORS policy.
**Required changes:**
- Add `app://kiosk` to `CORS_ORIGINS` in `.env.example` and deployment docs.
- Verify Electron `session.defaultSession` CSP allows outbound connections to `localhost:5007`.
**Acceptance criteria:** `fetch("http://localhost:5007/health")` from within Electron renderer returns 200 without CORS error.

---

### GAP-08: Proactive engagement controller calls local agent only
**File:** `src/hooks/useKiosk.js` (lines ~36–48 — proactiveRef logic)
**Owner:** Frontend
**Root cause:** The silence-detection and proactive prompt logic in `useKiosk.js` calls `say()` with hardcoded strings or routes through the local `reply()` function. There is no backend call for proactive messages.
**Required changes:**
- For proactive "attract mode" messages: use hardcoded strings or a small local set — acceptable since they are not product-specific.
- For proactive product questions after engagement: route through backend with a synthetic signal message, e.g. `{"message":"[PROACTIVE] Customer has been looking at drinkware for 15 seconds."}`.
- Rate-limit proactive backend calls (max 1 per 30 seconds).
**Acceptance criteria:** Proactive messages do not interrupt speech in progress. Backend-originated proactive replies pass factual number validation.

---

## MEDIUM PRIORITY GAPS (P2 — Required for production)

### GAP-09: No reconnection logic for backend failures
**File:** `src/lib/backend.js` (new file)
**Owner:** Frontend
**Root cause:** If the backend restarts or times out, the frontend has no fallback. A customer mid-conversation sees a blank caption or no response.
**Required changes:**
- WebSocket: auto-reconnect with exponential backoff (500ms → 2s → 5s, max 3 attempts).
- REST fallback: if WS unavailable, try `POST /chat` once.
- Timeout: if no `"done"` received in 15 seconds, show "I'm having trouble connecting. Please ask a store associate."
- Offline indicator: display subtle "Demo mode" badge when backend unreachable; use local `agent.js` as explicit fallback only when labeled.

---

### GAP-10: No B2B gifting state persistence across turns
**File:** `app/sessions.py`, `src/hooks/useKiosk.js`
**Owner:** Backend + Frontend
**Root cause:** LangGraph `MemorySaver` holds conversation history in memory (not persisted to disk or DB). If the backend restarts, all session state is lost. The frontend also has no sales_state tracking for quantity/budget/occasion.
**Required changes:**
- Backend: evaluate `SqliteSaver` or `PostgresSaver` for production checkpointing.
- Frontend: track `salesState` (quantity, budget, occasion, selected_product_sku) from backend tool call responses embedded in `"done"` messages.
- Backend: consider returning structured `sales_state` in a new `metadata` field on the `"done"` WebSocket message.

---

### GAP-11: No rate limiting on REST or WebSocket endpoints
**File:** `app/server.py`
**Owner:** Backend
**Root cause:** No rate limiting middleware exists. A misbehaving frontend or attacker could exhaust LLM API quota.
**Required changes:**
- Add per-session rate limit: max 20 messages/minute.
- Add global limit: max 100 concurrent WS connections.
- Return `{"type":"error","data":"Too many requests. Please wait."}` on WS; 429 on REST.

---

### GAP-12: useUserSignals face-detection signals treated as reliable emotional intent
**File:** `src/hooks/useUserSignals.js`, `src/lib/agent.js`
**Owner:** Frontend
**Root cause:** `useUserSignals` derives mood (`Happy`, `Sad`, `Confused`) from MediaPipe blendshapes and passes them to `reply()`. The audit requirement explicitly prohibits treating facial expression inference as reliable emotional intent. The mock `agent.js` silently ignores signals; any real backend integration must not use mood to gate product recommendations.
**Required changes:**
- Remove `mood`, `smile`, `sad`, `confused`, `surprised` from any backend-bound message payload.
- Keep only `present` (boolean) and `attention` (screen/away) as safe behavioral signals.
- Never include inferred emotion in the system prompt or user message to LangGraph.

---

### GAP-13: Frontend avatar/components directory not audited (missing from GitHub)
**File:** `src/components/avatar/`, `src/components/products/`
**Owner:** Frontend
**Root cause:** These directories are listed in the audit specification but were not found in the GitHub file tree. Only `public/avatar/greenie.glb` exists. The avatar rendering and caption display components need to be verified.
**Required changes:**
- Confirm avatar component wires caption text to the 3D model speech animation.
- Confirm avatar receives `speaking` state from `useSpeech` to sync lip animation.
- Confirm product card components accept the normalized backend schema fields.

---

## LOW PRIORITY GAPS (P3 — Future)

### GAP-14: No kiosk authentication / device identity
**File:** `app/server.py`, `main.js`, `preload.js`
**Root cause:** Any device on the network can call `POST /chat`. No device identity or kiosk token exists.
**Required changes:** Generate a `KIOSK_SECRET` on first boot stored in `config.json`. Send as `X-Kiosk-ID` header. Backend validates.

### GAP-15: Checkout handoff URL not surfaced in frontend
**File:** `src/hooks/useKiosk.js`
**Root cause:** Backend `generate_checkout_handoff` tool returns `url` and `action: "review_product_on_website"`. Frontend never reads this from the AI reply and never renders a QR code or "View on website" button.

### GAP-16: Hindi/Hinglish TTS not tested
**File:** `src/hooks/useSpeech.js`
**Root cause:** Backend system prompt says "Follow the customer's English, Hindi or Hinglish preference." The Web Speech API `SpeechSynthesis` language must be set to `hi-IN` for Hindi text. No language detection or voice selection logic exists in `useSpeech.js`.

### GAP-17: No end-to-end test suite spanning frontend and backend
**Root cause:** Backend has `tests/` directory with unit tests. Frontend has no test files. No integration test automates a frontend → backend → reply round trip.

---

## GAP SUMMARY TABLE

| ID | Title | Priority | Owner | Phase |
|---|---|---|---|---|
| GAP-01 | No AI backend integration | P0 | Frontend | Phase 2 |
| GAP-02 | Product ID mismatch | P0 | Frontend | Phase 3 |
| GAP-03 | Invented product data | P0 | Frontend | Phase 3 |
| GAP-04 | Session ID not persisted | P0 | Frontend+BE | Phase 2 |
| GAP-05 | No WebSocket client | P1 | Frontend | Phase 2 |
| GAP-06 | useSpeech not connected | P1 | Frontend | Phase 4 |
| GAP-07 | CORS blocks Electron | P1 | Backend | Phase 7 |
| GAP-08 | Proactive engine local-only | P1 | Frontend | Phase 5 |
| GAP-09 | No reconnection logic | P2 | Frontend | Phase 2 |
| GAP-10 | No B2B state persistence | P2 | Both | Phase 6 |
| GAP-11 | No rate limiting | P2 | Backend | Phase 7 |
| GAP-12 | Emotion signals misused | P2 | Frontend | Phase 4 |
| GAP-13 | Avatar components missing | P2 | Frontend | Phase 4 |
| GAP-14 | No device auth | P3 | Both | Phase 7 |
| GAP-15 | Checkout URL not shown | P3 | Frontend | Phase 6 |
| GAP-16 | Hindi TTS untested | P3 | Frontend | Phase 4 |
| GAP-17 | No E2E test suite | P3 | Both | Phase 8 |
