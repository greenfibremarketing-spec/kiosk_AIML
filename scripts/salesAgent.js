/**
 * salesAgent.js  —  Greeny Kiosk 5-Step Fast Flow (WebSocket only)
 *
 * How it works:
 *  1. WAKE        → customer taps Start / says hello  → server sends SHOW_GREETING
 *  2. NAME        → server asks name                  → server sends ASK_NAME
 *  3. CATEGORY    → big tappable buttons              → server sends SHOW_CATEGORIES
 *  4. PRODUCT     → one product at a time             → server sends SHOW_PRODUCT_CARD
 *  5. BUY/GOODBYE → confirm → checkout handoff        → server sends CHECKOUT_HANDOFF + GOODBYE
 *
 * Always on screen: Call staff · Help · Start over
 *
 * Silence detection (client side):
 *  • 8 s of no interaction → server will push type:"ping_silence"
 *  • 2nd ping              → server will push type:"auto_reset"
 *  The client handles these to show a "Still there?" banner and auto-reset.
 *
 * Usage:
 *   const agent = new SalesAgent({ wsUrl: "ws://localhost:5007/ws", sessionId: "kiosk-01" });
 *   agent.on("action", (screenAction) => renderScreen(screenAction));
 *   agent.on("token",  (word)         => appendToAvatar(word));
 *   agent.on("done",   (reply)        => finaliseAvatarSpeech(reply));
 *   agent.on("error",  (msg)          => showErrorBanner(msg));
 *   agent.on("silence_ping",  ()      => showStillThereBanner());
 *   agent.on("auto_reset",    ()      => resetUI());
 *   agent.connect();
 */

export class SalesAgent extends EventTarget {
  /**
   * @param {{ wsUrl: string, sessionId: string, kioskId?: string }} opts
   */
  constructor({ wsUrl, sessionId, kioskId = "kiosk-01" } = {}) {
    super();
    this._wsUrl     = wsUrl;
    this._sessionId = sessionId;
    this._kioskId   = kioskId;
    this._ws        = null;
    this._turnId    = 0;
    this._silenceTimer = null;
    this._silenceMs = 8000;          // 8 s before pinging
    this._connected = false;
  }


  // ─── Public API ──────────────────────────────────────────────────────────

  /** Open the WebSocket and start the greeting on connect. */
  connect() {
    const url = `${this._wsUrl}/${this._sessionId}?v=2&kiosk_id=${this._kioskId}`;
    this._ws  = new WebSocket(url);

    this._ws.onopen = () => {
      this._connected = true;
      this._emit("connected");
      // Trigger greeting immediately
      this.send("hello");
      this._resetSilenceTimer();
    };

    this._ws.onmessage = (evt) => {
      this._resetSilenceTimer();
      try {
        const frame = JSON.parse(evt.data);
        this._handleFrame(frame);
      } catch (e) {
        this._emit("error", "Malformed server frame.");
      }
    };

    this._ws.onclose = () => {
      this._connected = false;
      this._clearSilenceTimer();
      this._emit("disconnected");
    };

    this._ws.onerror = () => {
      this._emit("error", "Connection error. Please try again.");
    };
  }

  /** Disconnect cleanly. */
  disconnect() {
    this._clearSilenceTimer();
    if (this._ws) this._ws.close();
  }

  /**
   * Send a plain text or voice message.
   * @param {string} text
   */
  send(text) {
    this._sendFrame({ v: 2, message: text.trim(), turn_id: this._nextTurnId() });
  }

  /**
   * Send a button action (e.g. "SELECT_CATEGORY:drinkware", "NEXT_PRODUCT", "BUY:GF:BTL:VIORA").
   * The backend fast_flow.py reads the message field directly.
   * @param {string} action  e.g. "NEXT_PRODUCT" or "SELECT_CATEGORY:drinkware"
   */
  button(action) {
    // Guided actions that need the structured action field
    const guidedMap = {
      RESTART:         { action: "RESTART" },
      SKIP_NAME:       { action: "SKIP_NAME" },
      SHOW_ALL:        { action: "SHOW_ALL" },
      CALL_STAFF:      { message: "call staff" },
      HELP:            { message: "help" },
      GOODBYE:         { message: "goodbye" },
      CHECKOUT:        { message: "checkout" },
    };

    const tid = this._nextTurnId();

    if (guidedMap[action]) {
      this._sendFrame({ v: 2, ...guidedMap[action], turn_id: tid });
      return;
    }

    // "SELECT_CATEGORY:drinkware" → { action:"SELECT_CATEGORY", category_id:"drinkware" }
    if (action.startsWith("SELECT_CATEGORY:")) {
      const cat = action.split(":")[1];
      this._sendFrame({ v: 2, action: "SELECT_CATEGORY", category_id: cat, turn_id: tid });
      return;
    }

    // "SELECT_PRODUCT:GF:BTL:VIORA" → { action:"SELECT_PRODUCT", sku:"GF:BTL:VIORA" }
    if (action.startsWith("SELECT_PRODUCT:")) {
      const sku = action.slice("SELECT_PRODUCT:".length);
      this._sendFrame({ v: 2, action: "SELECT_PRODUCT", sku, turn_id: tid });
      return;
    }

    // Everything else (NEXT_PRODUCT, CHEAPER, COMPARE, BUY:sku, SHOW_ALL etc.)
    // send as a plain message — fast_flow.py parses the text
    this._sendFrame({ v: 2, message: action.toLowerCase().replace(/_/g, " "), turn_id: tid });
  }

  /**
   * Handle SPEECH_DONE from the TTS engine (avatar finished speaking greeting).
   * @param {string} speechId  — the speech_id returned in the SHOW_GREETING action
   */
  speechDone(speechId) {
    this._sendFrame({ v: 2, action: "SPEECH_DONE", speech_id: speechId, turn_id: this._nextTurnId() });
  }

  /** Cancel the currently in-progress turn. */
  cancel() {
    this._sendRaw({ type: "cancel", turn_id: this._currentTurnId() });
  }

  /** Keepalive ping. */
  ping() {
    this._sendRaw({ type: "ping" });
  }


  // ─── Frame handler ───────────────────────────────────────────────────────

  _handleFrame(frame) {
    const { type, data, meta } = frame;

    switch (type) {
      case "token":
        this._emit("token", data);
        break;

      case "sentence":
        this._emit("sentence", data);
        break;

      case "action":
        this._emit("action", data);
        break;

      case "done":
        this._emit("done", data, meta);
        break;

      case "error":
        this._emit("error", data);
        break;

      case "cancelled":
        this._emit("cancelled");
        break;

      case "pong":
        // keepalive — no action needed
        break;

      // ── Fast-flow specific push frames ────────────────────────────────────
      case "ping_silence":
        this._emit("silence_ping", data);
        break;

      case "auto_reset":
        this._emit("auto_reset", data);
        break;

      default:
        // Ignore unknown frame types for forward compat
        break;
    }
  }


  // ─── Silence timer ───────────────────────────────────────────────────────

  _resetSilenceTimer() {
    this._clearSilenceTimer();
    this._silenceTimer = setTimeout(() => {
      // No activity for 8 s — notify backend (it will push ping_silence or auto_reset)
      this.ping();           // keep connection alive
      this._emit("silence"); // let the UI show the "Still there?" banner locally too
    }, this._silenceMs);
  }

  _clearSilenceTimer() {
    if (this._silenceTimer) {
      clearTimeout(this._silenceTimer);
      this._silenceTimer = null;
    }
  }


  // ─── Internal helpers ────────────────────────────────────────────────────

  _tid = 0;
  _nextTurnId()    { return `t${++this._tid}`; }
  _currentTurnId() { return `t${this._tid}`; }

  _sendFrame(obj) {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
    this._ws.send(JSON.stringify(obj));
  }

  _sendRaw(obj) {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
    this._ws.send(JSON.stringify(obj));
  }

  /**
   * EventTarget helper — emit a custom event with optional detail.
   * Callers use agent.on("action", handler) via agent.addEventListener.
   */
  _emit(name, ...args) {
    this.dispatchEvent(Object.assign(new Event(name), { detail: args }));
  }

  /**
   * Shorthand: agent.on("action", fn) instead of agent.addEventListener("action", fn).
   */
  on(event, fn) {
    this.addEventListener(event, (e) => fn(...(e.detail ?? [])));
    return this;
  }
}


// ─── Usage example (remove or adapt for your framework) ─────────────────────
//
// const agent = new SalesAgent({
//   wsUrl:     "ws://localhost:5007/ws",
//   sessionId: "kiosk-floor-01",
//   kioskId:   "kiosk-01",
// });
//
// agent
//   .on("action",       renderScreenAction)   // update UI buttons & product cards
//   .on("token",        appendAvatarWord)      // stream avatar speech word by word
//   .on("done",         finaliseAvatarSpeech)  // full reply ready for TTS
//   .on("silence_ping", showStillThereBanner)  // "Still there?" overlay
//   .on("auto_reset",   resetKiosk)            // session timed out — reset UI
//   .on("error",        showErrorBanner)
//   .on("disconnected", attemptReconnect);
//
// agent.connect();
//
// // Button tap:
// document.querySelectorAll("[data-action]").forEach(btn => {
//   btn.addEventListener("click", () => agent.button(btn.dataset.action));
// });
//
// // TTS onend:
// ttsPlayer.onended = () => {
//   if (currentSpeechId) agent.speechDone(currentSpeechId);
// };
