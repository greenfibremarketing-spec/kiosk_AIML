# Greeny guided conversation implementation

Updated after the 9 October 2026 frontend-contract audit.

The authoritative payloads, test results and limitations are now in
[GREENY_FRONTEND_CONTRACT_FINAL.md](GREENY_FRONTEND_CONTRACT_FINAL.md).
The previous version of this guide contained incorrect authentication field names,
unimplemented SET_NAME/SHOW_ALL examples, invented product example SKUs, unguarded
speech acknowledgement semantics, and unverified completion/test-count claims.
Those statements are superseded; they are not deployment evidence.

The existing FastAPI REST and WebSocket handlers share ConversationInput validation
in app/guided_contract.py. The existing LangGraph persists guided sales state,
pending greeting tokens and recent event IDs. The existing sales engine handles
commands and conversational transitions; the existing read-only repository supplies
fresh product facts and category options. No replacement framework was introduced.

Use POST /chat (or /v1/chat) and WS /ws/{session_id}?v=2. Do not call /api/flow/step
or /api/flow/interpret: neither route exists. WS authentication uses token=,
X-Kiosk-Token or Bearer, not ticket= or X-Kiosk-Ticket.

Greeting progression requires the matching server-issued speech_id in SPEECH_DONE.
ASK_NAME is a backend action. SET_NAME and SKIP_NAME are client commands. Every
structured client command requires a turn_id. REST commands also require session_id.
See the final contract for exact fields and examples.

Guided states are GREETING, ASK_NAME, ASK_CATEGORY, SHOW_PRODUCTS, PRODUCT_DETAIL,
PRODUCT_QUESTIONS, B2B_QUALIFICATION, QUOTE_REQUEST and HUMAN_HANDOFF. Legacy states
remain supported; budget changes can still emit QUALIFICATION. Consent does not
mean a lead was submitted or that the conversation automatically reached COMPLETED.

Actual repository-observed example parent SKU:
GF:6ac33bf78072ec78d51387f0 (Viora Water Bottle - 400 ml).
Actual example variant SKU:
GF:6ac33bf78072ec78d51387f0:6ac33bf7e3e99cc6fbcfcc2b (Parrot green).
Always select IDs from current backend responses. The recorded identity/detail
snapshot is documentation, not a current inventory or price cache.

The original frontend-team specification remains unavailable for final comparison.
Physical-kiosk, production deployment, database and live-model acceptance are not
established by this local backend implementation.
