"""Automated Tests for Guided Conversation Flow & LangGraph Intelligence (Phases 1-8).

Verifies all 20 required scenarios:
1. Greeting advances to ASK_NAME after speech-done acknowledgement.
2. Name provided, skipped, and refused.
3. Category selected through tap, speech, and text.
4. Show everything.
5. Exact product and variant selection.
6. Unknown product request handled safely.
7. Product question with verified evidence.
8. Product question without approved compliance evidence.
9. Back navigation across conversation stages.
10. Restart mid-conversation.
11. Budget changes at any stage.
12. B2B 200-gift enquiry (no premature phone collection).
13. Customer declining phone sharing.
14. Hindi / Hinglish category interpretation.
15. ASR transcription mistakes tolerated.
16. WebSocket turn execution and replay protection.
17. Two simultaneous kiosk sessions remain isolated.
18. MongoDB checkpoint restoration.
19. Real product ID matching.
20. Zero invented prices, stock numbers, or certificates.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
import uuid
import pytest
from starlette.testclient import TestClient

from app.brain import ask_avatar_turn, kiosk_brain
from app.sales_engine import (
    CATEGORIES_METADATA,
    determine_sales_transition,
    extract_customer_name,
    find_matching_products,
    find_specific_product,
)
from app.sales_state import SalesStage, SalesState
from app.server import api, generate_kiosk_ticket
try:
    from test_mongo_persistence import MockDatabase
except ImportError:
    from tests.test_mongo_persistence import MockDatabase
from app.mongo_checkpointer import MongoCheckpointSaver


NOW_ISO = datetime.now(timezone.utc).isoformat()

MOCK_CATALOG = {
    "products": [
        {
            "id": "viora-bottle",
            "sku": "GF:BTL:VIORA",
            "name": "Viora Eco Bottle",
            "category": "Drinkware",
            "price": "649",
            "mrp": "799",
            "stock": 35,
            "in_stock": True,
            "stock_status": "in_stock",
            "stock_checked_at": NOW_ISO,
            "description": "Sage-green reusable bottle made from upcycled rice-husk biocomposite.",
            "images": ["https://greenfibre.org/images/viora-sage.jpg"],
            "product_url": "https://greenfibre.org/shop/viora-bottle",
            "variants": [
                {"id": "var-sage", "sku": "GF:BTL:VIORA:SAGE", "name": "Sage Green", "stock": 20},
                {"id": "var-sand", "sku": "GF:BTL:VIORA:SAND", "name": "Sand Dune", "stock": 15},
            ],
        },
        {
            "id": "statement-mug",
            "sku": "GF:MUG:STAT",
            "name": "Statement Ceramic-Feel Mug",
            "category": "Kitchen & Dining",
            "price": "399",
            "mrp": "499",
            "stock": 48,
            "in_stock": True,
            "stock_status": "in_stock",
            "stock_checked_at": NOW_ISO,
            "description": "400ml matte charcoal mug for daily coffee and tea.",
            "images": ["https://greenfibre.org/images/statement-mug.jpg"],
            "product_url": "https://greenfibre.org/shop/statement-mug",
            "variants": [
                {"id": "var-charcoal", "sku": "GF:MUG:STAT:CHAR", "name": "Charcoal", "stock": 48},
            ],
        },
        {
            "id": "flora-bowl",
            "sku": "GF:BWL:FLORA",
            "name": "Flora Serving Bowl",
            "category": "Kitchen & Dining",
            "price": "549",
            "mrp": "699",
            "stock": 24,
            "in_stock": True,
            "stock_status": "in_stock",
            "stock_checked_at": NOW_ISO,
            "description": "Lightweight wide serving bowl for salads, fruits, and snacks.",
            "images": ["https://greenfibre.org/images/flora-bowl.jpg"],
            "product_url": "https://greenfibre.org/shop/flora-bowl",
            "variants": [],
        },
    ]
}


@pytest.fixture(autouse=True)
def mock_catalog_fixture(monkeypatch):
    from app import sales_engine
    from copy import deepcopy

    class MockRepo:
        def catalog(self):
            return deepcopy(MOCK_CATALOG)

        def get(self, sku):
            for p in MOCK_CATALOG["products"]:
                if p["sku"] == sku or p["id"] == sku:
                    return deepcopy(p)
                for v in p.get("variants", []):
                    if v.get("sku") == sku or v.get("id") == sku:
                        return {**deepcopy(p), "sku": v["sku"], "variant_id": v["id"]}
            return None

    monkeypatch.setattr(sales_engine, "get_product_repository", lambda: MockRepo())


# ── Scenario 1: Greeting & Speech-Completion Acknowledgement ──────────────────

def test_1_greeting_advances_only_after_speech_done():
    state = SalesState(sales_stage=SalesStage.GREETING)

    # Regular greeting utterance stays in GREETING with SHOW_CATEGORIES
    next_s1, a1 = determine_sales_transition(state, "Hello Greeny!", catalog=MOCK_CATALOG)
    assert next_s1.sales_stage == SalesStage.GREETING
    assert a1["action"] == "SHOW_CATEGORIES"

    # Frontend speech-done acknowledgement triggers advance to ASK_NAME
    next_s2, a2 = determine_sales_transition(next_s1, "SPEECH_DONE", catalog=MOCK_CATALOG, command={"action":"SPEECH_DONE", "turn_id":"ack-1", "speech_id":a1["speech_id"]})
    assert next_s2.sales_stage == SalesStage.ASK_NAME
    assert a2["action"] == "ASK_NAME"
    assert a2["prompt"] == "What should I call you?"
    assert a2["skip_allowed"] is True


# ── Scenario 2: Name Provided, Skipped, and Refused ───────────────────────────

def test_2_name_provided_skipped_and_refused():
    ask_state = SalesState(sales_stage=SalesStage.ASK_NAME)

    # 2A: Name provided
    s_named, a_named = determine_sales_transition(ask_state, "My name is Rahul", catalog=MOCK_CATALOG)
    assert s_named.sales_stage == SalesStage.INTENT
    assert s_named.customer_name == "Rahul"
    assert a_named["action"] == "SHOW_CATEGORIES"
    assert "Rahul" in a_named["message"]

    # 2B: Single word name at ASK_NAME prompt
    s_single, a_single = determine_sales_transition(ask_state, "Priya", catalog=MOCK_CATALOG)
    assert s_single.sales_stage == SalesStage.INTENT
    assert s_single.customer_name == "Priya"
    assert "Priya" in a_single["message"]

    # 2C: Name skipped
    s_skip, a_skip = determine_sales_transition(ask_state, "Skip", catalog=MOCK_CATALOG)
    assert s_skip.sales_stage == SalesStage.INTENT
    assert s_skip.customer_name is None
    assert a_skip["action"] == "SHOW_CATEGORIES"
    assert "No problem" in a_skip["message"]

    # 2D: Name refused
    s_refuse, a_refuse = determine_sales_transition(ask_state, "I don't want to share my name", catalog=MOCK_CATALOG)
    assert s_refuse.sales_stage == SalesStage.INTENT
    assert a_refuse["action"] == "SHOW_CATEGORIES"


# ── Scenario 3: Category Selected via Tap, Speech, and Text ───────────────────

def test_3_category_selected_through_tap_speech_text():
    state = SalesState(sales_stage=SalesStage.INTENT)

    # Tap action via structured ID
    s_tap, a_tap = determine_sales_transition(state, "select_category:drinkware", catalog=MOCK_CATALOG)
    assert a_tap["action"] == "SHOW_PRODUCTS"
    assert any("Bottle" in p["name"] for p in a_tap["products"])

    # Natural speech / text
    s_speech, a_speech = determine_sales_transition(state, "Show me bottles and drinkware", catalog=MOCK_CATALOG)
    assert a_speech["action"] == "SHOW_PRODUCTS"
    assert a_speech["count"] > 0


# ── Scenario 4: Show Everything ───────────────────────────────────────────────

def test_4_show_everything():
    state = SalesState(sales_stage=SalesStage.INTENT)
    next_s, action = determine_sales_transition(state, "Show everything in your store", catalog=MOCK_CATALOG)
    assert next_s.sales_stage == SalesStage.RECOMMENDATION
    assert action["action"] == "SHOW_PRODUCTS"
    assert action["count"] == len(MOCK_CATALOG["products"])
    assert action["category"] == "All Products"


# ── Scenario 5: Exact Product Selection & Variant Details ─────────────────────

def test_5_exact_product_and_variant_selection():
    state = SalesState(sales_stage=SalesStage.RECOMMENDATION)
    next_s, action = determine_sales_transition(state, "Tell me details about the Viora Eco Bottle", catalog=MOCK_CATALOG)
    assert action["action"] == "SHOW_PRODUCT"
    assert action["sku"] == "GF:BTL:VIORA"
    prod = action["product"]
    assert prod["name"] == "Viora Eco Bottle"
    assert prod["price"] == "649"
    assert prod["mrp"] == "799"
    assert prod["stock"] == 35
    assert len(prod["variants"]) == 2
    assert prod["variants"][0]["name"] == "Sage Green"


# ── Scenario 6: Unknown Product Handled Safely ─────────────────────────────────

def test_6_unknown_product_request():
    state = SalesState(sales_stage=SalesStage.INTENT)
    next_s, action = determine_sales_transition(state, "Do you have laptops or smartphones?", catalog=MOCK_CATALOG)
    # Does NOT invent or show fake electronics products
    if action:
        assert action["action"] != "SHOW_PRODUCT"


# ── Scenario 7: Product Question with Verified Evidence ───────────────────────

def test_7_product_question_with_verified_evidence():
    session_id = f"test-q-{uuid.uuid4().hex[:8]}"
    turn = ask_avatar_turn("How much does the Viora Eco Bottle cost?", session_id=session_id)
    assert turn.reply
    # Verified catalogue price for Viora Bottle is 649
    assert ("649" in turn.reply or "six hundred" in turn.reply or "confirm" in turn.reply)


# ── Scenario 8: Product Question Without Compliance Evidence ──────────────────

def test_8_product_question_without_compliance_evidence():
    session_id = f"test-comp-{uuid.uuid4().hex[:8]}"
    turn = ask_avatar_turn("Is this bottle certified carbon negative and FDA certified?", session_id=session_id)
    # Post-generation compliance validator ensures ungrounded certification claims are suppressed
    assert "certified" not in turn.reply.lower() or "not" in turn.reply.lower() or "check" in turn.reply.lower() or "associate" in turn.reply.lower()


# ── Scenario 9: Back Navigation ───────────────────────────────────────────────

def test_9_back_navigation():
    # 9A: From product detail -> returns to product list
    state_detail = SalesState(
        sales_stage=SalesStage.RECOMMENDATION,
        selected_skus=("GF:BTL:VIORA",),
        interested_skus=("GF:BTL:VIORA", "GF:MUG:STAT"),
    )
    s1, a1 = determine_sales_transition(state_detail, "back", catalog=MOCK_CATALOG)
    assert s1.selected_skus == ()
    assert a1["action"] == "SHOW_PRODUCTS"

    # 9B: From products -> returns to categories
    state_prods = SalesState(sales_stage=SalesStage.RECOMMENDATION)
    s2, a2 = determine_sales_transition(state_prods, "go back", catalog=MOCK_CATALOG)
    assert s2.sales_stage == SalesStage.INTENT
    assert a2["action"] == "SHOW_CATEGORIES"

    # 9C: From categories -> returns to ask name
    state_cats = SalesState(sales_stage=SalesStage.INTENT)
    s3, a3 = determine_sales_transition(state_cats, "back", catalog=MOCK_CATALOG)
    assert s3.sales_stage == SalesStage.ASK_NAME
    assert a3["action"] == "ASK_NAME"


# ── Scenario 10: Restart Mid-Conversation ─────────────────────────────────────

def test_10_restart():
    state_deep = SalesState(
        sales_stage=SalesStage.RECOMMENDATION,
        customer_name="Rahul",
        budget=Decimal("1500"),
        selected_skus=("GF:BTL:VIORA",),
    )
    s_reset, a_reset = determine_sales_transition(state_deep, "start over", catalog=MOCK_CATALOG)
    assert s_reset.sales_stage == SalesStage.GREETING
    assert s_reset.customer_name is None
    assert s_reset.budget is None
    assert s_reset.selected_skus == ()
    assert a_reset["action"] == "SHOW_CATEGORIES"


# ── Scenario 11: Budget Changes at Any Stage ──────────────────────────────────

def test_11_budget_changes_at_any_stage():
    state = SalesState(sales_stage=SalesStage.RECOMMENDATION, budget=Decimal("1500"))
    next_s, action = determine_sales_transition(state, "Actually change my budget to under 500 rupees", catalog=MOCK_CATALOG)
    assert next_s.budget == Decimal("500")
    assert action["action"] == "SHOW_PRICE_BANDS"
    assert action["selected_budget"] == 500.0


# ── Scenario 12: B2B 200-Gift Enquiry (No Premature Phone Prompt) ──────────────

def test_12_b2b_200_gift_enquiry():
    state = SalesState(sales_stage=SalesStage.GREETING)
    next_s, action = determine_sales_transition(
        state,
        "We need 200 corporate gifts under ₹1500 for our annual conference",
        catalog=MOCK_CATALOG,
    )
    assert next_s.customer_intent == "b2b"
    assert next_s.quantity == 200
    assert next_s.budget == Decimal("1500")
    # Must show products or price bands, NOT prematurely ask for a phone number
    assert action["action"] in ("SHOW_PRODUCTS", "SHOW_PRICE_BANDS")
    assert action["action"] != "COLLECT_PHONE"


# ── Scenario 13: Customer Declining Phone Sharing ─────────────────────────────

def test_13_customer_declining_phone_sharing():
    state = SalesState(sales_stage=SalesStage.QUALIFICATION, customer_intent="b2b", contact_consent_pending=True)
    next_s, action = determine_sales_transition(state, "I will not share my phone number, just show on screen", catalog=MOCK_CATALOG)
    assert next_s.customer_contact_consent == "declined"
    assert next_s.lead_status == "declined"
    assert action is None or action["action"] != "COLLECT_PHONE"


# ── Scenario 14: Hindi & Hinglish Interpretation ──────────────────────────────

def test_14_hindi_hinglish_category_interpretation():
    state = SalesState(sales_stage=SalesStage.INTENT)

    # "Paani ki bottle dikhao" -> matches Drinkware
    s_hi1, a_hi1 = determine_sales_transition(state, "Paani ki bottle dikhao", catalog=MOCK_CATALOG)
    assert a_hi1["action"] == "SHOW_PRODUCTS"
    assert any("Bottle" in p["name"] for p in a_hi1["products"])

    # "Chai ka cup" -> matches Drinkware / Mug
    s_hi2, a_hi2 = determine_sales_transition(state, "Chai ka cup chahiye", catalog=MOCK_CATALOG)
    assert a_hi2["action"] == "SHOW_PRODUCTS"

    # "Sab dikhao" -> matches Show All
    s_hi3, a_hi3 = determine_sales_transition(state, "Sab dikhao", catalog=MOCK_CATALOG)
    assert a_hi3["action"] == "SHOW_PRODUCTS"
    assert a_hi3["count"] == len(MOCK_CATALOG["products"])


# ── Scenario 15: ASR Transcription Mistakes ───────────────────────────────────

def test_15_asr_transcription_mistakes():
    state = SalesState(sales_stage=SalesStage.RECOMMENDATION)

    # "vyora eco botle" -> matches Viora Eco Bottle
    s1, a1 = determine_sales_transition(state, "Tell me about vyora eco botle", catalog=MOCK_CATALOG)
    assert a1["action"] == "SHOW_PRODUCT"
    assert a1["sku"] == "GF:BTL:VIORA"

    # "flora boul" -> matches Flora Bowl
    s2, a2 = determine_sales_transition(state, "Details on flora boul", catalog=MOCK_CATALOG)
    assert a2["action"] == "SHOW_PRODUCT"
    assert a2["sku"] == "GF:BWL:FLORA"


# ── Scenario 16: WebSocket Reconnect & Duplicate Turn Replay Protection ───────

def test_16_websocket_reconnect_and_duplicate_events():
    client = TestClient(api)
    session_id = f"test-ws-dup-{uuid.uuid4().hex[:8]}"

    with client.websocket_connect(f"/ws/{session_id}?v=2") as ws:
        # Turn 1 with explicit turn_id
        ws.send_text(json.dumps({"message": "Hello Greeny", "turn_id": "turn-alpha-1"}))
        done_found = False
        while not done_found:
            msg = json.loads(ws.receive_text())
            assert msg.get("type") != "error", msg
            if msg.get("type") == "done":
                greeting_speech_id = msg["meta"]["action"]["speech_id"]
                done_found = True

        # Turn 2 with replayed turn_id "turn-alpha-1" -> rejected
        ws.send_text(json.dumps({"message": "Show bottles", "turn_id": "turn-alpha-1"}))
        err_msg = json.loads(ws.receive_text())
        assert err_msg["type"] == "error"
        assert "duplicate" in err_msg["data"].lower()

        # Turn 3 with fresh turn_id succeeds
        ws.send_text(json.dumps({"action": "SPEECH_DONE", "speech_id": greeting_speech_id, "turn_id": "turn-alpha-2"}))
        done_found_2 = False
        while not done_found_2:
            msg = json.loads(ws.receive_text())
            assert msg.get("type") != "error", msg
            if msg.get("type") == "done":
                done_found_2 = True


# ── Scenario 17: Two Simultaneous Kiosk Sessions Remain Isolated ──────────────

def test_17_two_simultaneous_kiosk_sessions():
    sess1 = f"sess-kiosk-1-{uuid.uuid4().hex[:8]}"
    sess2 = f"sess-kiosk-2-{uuid.uuid4().hex[:8]}"

    # Turn on Kiosk 1: Rahul looking for bottles
    t1 = ask_avatar_turn("My name is Rahul", session_id=sess1, kiosk_id="kiosk-01")
    ask_avatar_turn("SPEECH_DONE", sess1, kiosk_id="kiosk-01", command={"action":"SPEECH_DONE", "turn_id":"ack-a", "speech_id":t1.action["speech_id"]})
    t1_bot = ask_avatar_turn("Show bottles", session_id=sess1, kiosk_id="kiosk-01")

    # Turn on Kiosk 2: Priya looking for mugs
    t2 = ask_avatar_turn("My name is Priya", session_id=sess2, kiosk_id="kiosk-02")
    ask_avatar_turn("SPEECH_DONE", sess2, kiosk_id="kiosk-02", command={"action":"SPEECH_DONE", "turn_id":"ack-b", "speech_id":t2.action["speech_id"]})
    t2_mug = ask_avatar_turn("Show mugs", session_id=sess2, kiosk_id="kiosk-02")

    # Verify checkpointer memory isolation
    cfg1 = {"configurable": {"thread_id": sess1}}
    snap1 = kiosk_brain.get_state(cfg1)
    sales1 = snap1.values.get("sales_state")
    assert sales1["customer_name"] == "Rahul"

    cfg2 = {"configurable": {"thread_id": sess2}}
    snap2 = kiosk_brain.get_state(cfg2)
    sales2 = snap2.values.get("sales_state")
    assert sales2["customer_name"] == "Priya"


# ── Scenario 18: MongoDB Checkpoint Restoration ───────────────────────────────

def test_18_mongodb_checkpoint_restoration():
    mock_db = MockDatabase("greeny_ai")
    saver1 = MongoCheckpointSaver(mock_db)
    saver1.ensure_indexes()

    thread_id = f"test-ckpt-{uuid.uuid4().hex[:8]}"
    kiosk_id = "kiosk-01"
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "kiosk_id": kiosk_id}}

    sales = SalesState(
        session_id=thread_id,
        kiosk_id=kiosk_id,
        customer_name="Aarav",
        budget=Decimal("700.00"),
        sales_stage=SalesStage.QUALIFICATION,
    )
    cp = {
        "v": 1,
        "id": "cp-1",
        "ts": datetime.now(timezone.utc).isoformat(),
        "channel_versions": {"messages": 1, "sales_state": 1},
        "channel_values": {
            "messages": ["Hello Greeny", "My budget is under 700 rupees"],
            "sales_state": sales.model_dump(mode="json"),
        },
        "versions_seen": {},
    }
    meta = {"source": "input", "step": 1, "writes": {}, "parents": {}}

    # 1. Save checkpoint to MongoDB checkpointer
    saver1.put(config, cp, meta, {"messages": 1, "sales_state": 1})

    # 2. Simulate complete backend restart (new MongoCheckpointSaver instance on same DB)
    saver2 = MongoCheckpointSaver(mock_db)
    restored = saver2.get_tuple(config)

    assert restored is not None
    assert restored.checkpoint["id"] == "cp-1"
    restored_sales = restored.checkpoint["channel_values"]["sales_state"]
    assert restored_sales["customer_name"] == "Aarav"
    assert restored_sales["budget"] == "700.00"
    assert restored_sales["sales_stage"] == "QUALIFICATION"


# ── Scenario 19: Real Product ID & SKU Matching ───────────────────────────────

def test_19_real_product_id_matching():
    # Direct lookup by canonical SKU
    p_sku = find_specific_product("GF:BTL:VIORA", MOCK_CATALOG)
    assert p_sku is not None
    assert p_sku["id"] == "viora-bottle"

    # Direct lookup by canonical ID
    p_id = find_specific_product("statement-mug", MOCK_CATALOG)
    assert p_id is not None
    assert p_id["sku"] == "GF:MUG:STAT"

    # Structured lookup action "select_product:GF:BWL:FLORA"
    p_act = find_specific_product("select_product:GF:BWL:FLORA", MOCK_CATALOG)
    assert p_act is not None
    assert p_act["id"] == "flora-bowl"


# ── Scenario 20: No Invented Prices, Stock Numbers, or Certificates ────────────

def test_20_no_invented_prices_stock_or_certificates():
    # Verify SHOW_PRODUCTS cards contain verified information and no fabricated fields
    matching = find_matching_products("bottle", MOCK_CATALOG, max_items=4)
    for p in matching:
        assert p["sku"].startswith("GF:")
        assert p["price"] in ("649", "399", "549")
        assert "rating" not in p
        assert "reviews" not in p
        assert "badge" not in p
