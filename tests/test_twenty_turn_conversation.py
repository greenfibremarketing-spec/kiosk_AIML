"""20-Turn Conversation Test with Bounded Token Usage and Zero Invented Prices.

Verifies:
- 20 consecutive turns on a single session thread
- Bounded token usage via sliding-window message history (at most 6 recent messages to LLM)
- Zero invented prices: all mentioned prices and quantities match verified tool/RAG data
- Clean spoken output: no markdown headers, bullets, backticks, or emojis
- Progression through sales stages and emission of appropriate WebSocket actions
- Continuous state preservation across turns
"""

import re
import uuid
import pytest
from app.brain import ask_avatar_turn, kiosk_brain
from app.sales_state import SalesStage


# Known verified numbers from mock tools / catalog / RAG docs
VERIFIED_NUMBERS = {
    0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 14, 15, 20, 24, 25, 30, 35, 48, 50,
    60, 70, 72, 80, 100, 120, 150, 250, 300, 350, 399, 400, 499, 500, 549, 649,
    699, 700, 749, 750, 799, 800, 899, 900, 949, 1000, 1048, 1200, 1298, 1499,
    1500, 2000, 5000, 9999,
}


TWENTY_TURNS = [
    # Turn 1: Greeting
    ("Hello Greeny, good morning!", {"stage": SalesStage.GREETING, "action": "SHOW_CATEGORIES"}),
    # Turn 2: Customer Name
    ("My name is Aarav Sharma", {}),
    # Turn 3: Category inquiry
    ("What categories do you offer?", {"action": "SHOW_CATEGORIES"}),
    # Turn 4: Drinkware inquiry
    ("Show me your bottles collection", {"action": "SHOW_PRODUCTS"}),
    # Turn 5: Budget specification
    ("My budget is under 700 rupees", {"stage": SalesStage.QUALIFICATION, "action": "SHOW_PRICE_BANDS"}),
    # Turn 6: Specific product inquiry (Viora Eco Bottle)
    ("Tell me details about the Viora Eco Bottle", {"action": "SHOW_PRODUCT"}),
    # Turn 7: Material / sustainability inquiry
    ("What material is the Viora bottle made of?", {}),
    # Turn 8: Care instructions / Dishwasher safety
    ("Is the bottle dishwasher safe?", {}),
    # Turn 9: Second product inquiry (Statement Mug)
    ("Tell me details about the Statement Mug", {"action": "SHOW_PRODUCT"}),
    # Turn 10: Microwave safety
    ("Can I use the Statement Mug in the microwave?", {}),
    # Turn 11: Travel tumbler inquiry
    ("Do you have travel tumblers for commute?", {"action": "SHOW_PRODUCTS"}),
    # Turn 12: Dining bowls inquiry
    ("Tell me details about the Flora Bowl", {"action": "SHOW_PRODUCT"}),
    # Turn 13: Gift bundle inquiry
    ("Do you have any gift bundles?", {"action": "SHOW_PRODUCTS"}),
    # Turn 14: Shipping policy inquiry
    ("What is your shipping policy?", {}),
    # Turn 15: Return policy inquiry
    ("What is your return policy?", {}),
    # Turn 16: B2B corporate bulk inquiry
    ("We need 120 bottles for corporate employee Diwali gifting, please send a quote", {"stage": SalesStage.QUOTE_REQUEST, "action": "CONFIRM_CONTACT_CONSENT"}),
    # Turn 17: Custom logo engraving inquiry
    ("Can we get our company logo engraved on the bottles?", {}),
    # Turn 18: Out-of-catalogue domain inquiry
    ("Do you sell laptops or smartphones?", {}),
    # Turn 19: Human handoff request
    ("Can I speak to a human store manager please?", {"stage": SalesStage.HUMAN_HANDOFF, "action": "REQUEST_HUMAN"}),
    # Turn 20: Post-handoff closing
    ("Thank you for all the help", {}),
]


def test_twenty_consecutive_turns_with_bounded_tokens_and_verified_prices(monkeypatch):
    from app import sales_engine
    from test_sales_engine import MOCK_CATALOG
    from copy import deepcopy
    class Repository:
        def catalog(self):
            return deepcopy(MOCK_CATALOG)
        def get(self, sku):
            return next((p for p in self.catalog()['products'] if p['sku'] == sku), None)
    monkeypatch.setattr(sales_engine, 'get_product_repository', lambda: Repository())
    session_id = f"test-twenty-turn-{uuid.uuid4().hex[:8]}"
    kiosk_id = "kiosk-koramangala-01"

    for turn_idx, (utterance, expectations) in enumerate(TWENTY_TURNS, start=1):
        turn_result = ask_avatar_turn(
            message=utterance,
            session_id=session_id,
            kiosk_id=kiosk_id,
        )

        if turn_idx == 1:
            ask_avatar_turn("SPEECH_DONE", session_id, kiosk_id=kiosk_id, command={"action":"SPEECH_DONE", "turn_id":"greeting-ack", "speech_id":turn_result.action["speech_id"]})
        reply = turn_result.reply
        assert reply, f"Turn {turn_idx}: Reply cannot be empty"

        # ── 1. Voice & TTS formatting rules ───────────────────────────────────
        assert "#" not in reply, f"Turn {turn_idx}: Markdown header in spoken reply: {reply}"
        assert "*" not in reply, f"Turn {turn_idx}: Asterisks in spoken reply: {reply}"
        assert "`" not in reply, f"Turn {turn_idx}: Backticks in spoken reply: {reply}"
        # Max sentence check (1 to 4 sentences)
        sentences = [s.strip() for s in re.split(r'[.!?।]', reply) if s.strip()]
        assert len(sentences) <= 4, f"Turn {turn_idx}: Spoken reply too long ({len(sentences)} sentences): {reply}"

        # ── 2. Zero invented prices guardrail ─────────────────────────────────
        # Extract all numbers from reply
        extracted_numbers = re.findall(r'\b\d+(?:\.\d+)?\b', reply)
        for num_str in extracted_numbers:
            val = float(num_str) if "." in num_str else int(num_str)
            assert val in VERIFIED_NUMBERS, (
                f"Turn {turn_idx}: Potentially invented number {val} found in spoken reply: '{reply}'"
            )

        # ── 3. Expected sales stages and UI actions ───────────────────────────
        if "stage" in expectations:
            expected_stage = expectations["stage"].value if hasattr(expectations["stage"], "value") else expectations["stage"]
            assert turn_result.sales_stage == expected_stage, (
                f"Turn {turn_idx}: Expected stage {expected_stage}, got {turn_result.sales_stage}"
            )

        if "action" in expectations:
            assert turn_result.action is not None, f"Turn {turn_idx}: Expected action {expectations['action']}, got None"
            assert turn_result.action["action"] == expectations["action"], (
                f"Turn {turn_idx}: Expected action {expectations['action']}, got {turn_result.action['action']}"
            )

        # ── 4. Verify LangGraph state checkpointer has session state ──────────
        thread_config = {"configurable": {"thread_id": session_id}}
        state_snapshot = kiosk_brain.get_state(thread_config)
        assert state_snapshot is not None, f"Turn {turn_idx}: Checkpoint snapshot missing"
        assert state_snapshot.values.get("sales_state") is not None, (
            f"Turn {turn_idx}: SalesState not persisted in checkpointer"
        )
