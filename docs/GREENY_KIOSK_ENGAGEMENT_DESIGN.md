# Proposed kiosk engagement events (not implemented)

This design follows authentication and session ownership work. Product browsing
does not authorize lead capture, cart submission, payments, audio recording or
storage of identity. Default event logs contain IDs/types/timings, not conversation text.

| Event | Backend responsibility | Frontend responsibility |
| --- | --- | --- |
| `idle_entered` | Return approved active showcase candidates; set idle state | Attract animation and rotating product cards |
| `showcase_tick` | Supply cached descriptive content with provenance; no LLM call per tick | Schedule display changes, pause while speaking or interacting |
| `visitor_interaction` | Issue contextual invitation only outside cooldown and quiet mode | Detect touch/proximity using device permissions; never infer identity |
| `product_selected` | Resolve exact SKU, establish context, return one useful invitation | Emit explicit selection and display product details |
| `conversation_started` | Suspend proactive invitations and create scoped session | Stop attract narration and focus conversation controls |
| `interrupt` | Cancel/mark obsolete the turn using generation ID; ignore late results | Immediately stop TTS; discard late tokens for that generation |
| `quiet_changed` | Save kiosk/session mute preference; suppress proactive speech | Stop audio immediately, keep captions and visual navigation |
| `idle_timeout` | Expire conversation according to policy; reset without leaking prior visitor data | Clear visitor-specific UI and return to attract mode |

Envelope: `{event_id, kiosk_id, session_id, type, generation_id, payload}`. The
authenticated principal determines the real kiosk ID; mismatches are rejected.
Use a shared event-id deduplication store with TTL, server clock for cooldowns,
bounded queues and configurable per-kiosk intervals. Client timestamps must not
reset cooldowns. Manual user questions interrupt invitations immediately; late
model responses cannot resume stopped speech.

Backend states: `IDLE`, `SHOWCASE`, `INVITING`, `CONVERSING`, `QUIET`.
Quiet is user-controlled and must not be automatically cleared by visitor events.
On timeout, determine visual idle independently from quiet/audio state. Never
play greetings to every passer-by or manufacture urgency.

Future deterministic tests: duplicate events, cooldown boundary, concurrent kiosks,
untrusted kiosk ID, mute persistence, interruption/late generation, offline client
reconnect, repeated product selection, idle expiry and stale catalogue data.

No frontend implementation is included in this Python patch. Sensors, animations,
microphone permissions, browser audio policies, QR rendering and TTS interruption
require frontend/device code. Checkout URLs must come from an approved backend
handoff adapter; frontend QR rendering does not authorize checkout actions.
