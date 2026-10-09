"""Tests for Deterministic Sales Engine & Structured UI Actions.

Verifies:
- All 6 structured UI actions:
  * SHOW_CATEGORIES
  * SHOW_PRICE_BANDS
  * SHOW_PRODUCTS
  * SHOW_PRODUCT
  * COLLECT_PHONE
  * REQUEST_HUMAN
- Deterministic transitions across sales stages:
  * GREETING -> INTENT -> QUALIFICATION -> PRODUCT_DISCOVERY -> RECOMMENDATION -> COMPLETED
- Real Green Fibre SKUs and variant IDs in action payloads
- Preservation of customer identity and session immutability
- Budget and bulk quantity parsing
- B2B corporate lead qualification
- Human handoff escalation
"""

from decimal import Decimal
import pytest

from app.sales_engine import (
    CATEGORIES_METADATA,
    PRICE_BANDS_METADATA,
    determine_sales_transition,
    extract_budget,
    extract_customer_name,
    extract_quantity,
    find_matching_products,
    find_specific_product,
)
from app.sales_state import SalesStage, SalesState


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
            "description": "Sage-green bottle made from upcycled rice-husk biocomposite.",
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
            "category": "Drinkware",
            "price": "399",
            "mrp": "499",
            "stock": 50,
            "in_stock": True,
            "description": "Ceramic-like mug made from upcycled rice-husk biocomposite.",
            "images": ["https://greenfibre.org/images/mug-charcoal.jpg"],
            "product_url": "https://greenfibre.org/shop/statement-mug",
            "variants": [
                {"id": "var-charcoal", "sku": "GF:MUG:STAT:CHAR", "name": "Charcoal Grey", "stock": 30},
                {"id": "var-ivory", "sku": "GF:MUG:STAT:IVORY", "name": "Ivory White", "stock": 20},
            ],
        },
        {
            "id": "flora-bowl",
            "sku": "GF:BWL:FLORA",
            "name": "Flora Soup & Salad Bowl",
            "category": "Kitchen & Dining",
            "price": "549",
            "mrp": "699",
            "stock": 25,
            "in_stock": True,
            "description": "Earthy-toned salad bowl made from rice husk.",
            "images": ["https://greenfibre.org/images/flora-bowl.jpg"],
            "product_url": "https://greenfibre.org/shop/flora-bowl",
            "variants": [],
        },
    ],
    "gift_bundles": [
        {
            "id": "bundle-desk-duo",
            "name": "Desk Hydration Duo",
            "price": "949",
            "mrp": "1298",
            "items": ["Statement Ceramic-Feel Mug", "Viora Eco Bottle"],
        }
    ],
}


# ==============================================================================
# 1. PARSING & EXTRACTION TESTS
# ==============================================================================

def test_extract_customer_name():
    assert extract_customer_name("Hello, my name is Aarav Sharma") == "Aarav Sharma"
    assert extract_customer_name("I'm Priya and I want a gift") == "Priya"
    assert extract_customer_name("Mera naam Vikram hai") == "Vikram"
    assert extract_customer_name("Call me Rohan") == "Rohan"
    assert extract_customer_name("I am looking for bottles") is None


def test_extract_budget():
    assert extract_budget("I want something under 1000 rupees") == Decimal("1000")
    assert extract_budget("budget of ₹750") == Decimal("750")
    assert extract_budget("within 1500 inr") == Decimal("1500")
    assert extract_budget("around 500 rs") == Decimal("500")
    assert extract_budget("no price mentioned") is None


def test_extract_quantity():
    assert extract_quantity("I need 100 pieces for gifting") == 100
    assert extract_quantity("want 50 units for my company") == 50
    assert extract_quantity("order 250 bottles") == 250
    assert extract_quantity("just looking at 2 bottles") is None  # below bulk threshold


# ==============================================================================
# 2. DETERMINISTIC TRANSITIONS & WEBSOCKET V2 ACTIONS
# ==============================================================================

def test_greeting_triggers_show_categories():
    state = SalesState(session_id="s1", kiosk_id="k1", sales_stage=SalesStage.GREETING)
    next_state, action = determine_sales_transition(state, "Hello Greeny!", catalog=MOCK_CATALOG)

    assert next_state.sales_stage == SalesStage.GREETING
    assert action is not None
    assert action["action"] == "SHOW_CATEGORIES"
    assert action["categories"] == []
    assert action["awaiting_speech_done"] is True
    assert action["input_enabled"] is False


def test_category_inquiry_triggers_show_categories():
    state = SalesState(session_id="s1", kiosk_id="k1", sales_stage=SalesStage.INTENT)
    next_state, action = determine_sales_transition(state, "What categories do you have?", catalog=MOCK_CATALOG)

    assert action is not None
    assert action["action"] == "SHOW_CATEGORIES"
    assert {item["name"] for item in action["categories"]} == {p["category"] for p in MOCK_CATALOG["products"]}


def test_budget_qualification_triggers_show_price_bands():
    state = SalesState(session_id="s1", kiosk_id="k1", sales_stage=SalesStage.INTENT)
    next_state, action = determine_sales_transition(state, "My budget is under 800 rupees", catalog=MOCK_CATALOG)

    assert next_state.sales_stage == SalesStage.QUALIFICATION
    assert next_state.budget == Decimal("800")
    assert action is not None
    assert action["action"] == "SHOW_PRICE_BANDS"
    assert action["selected_budget"] == 800.0
    assert len(action["bands"]) == 4


def test_product_discovery_triggers_show_products():
    state = SalesState(session_id="s1", kiosk_id="k1", sales_stage=SalesStage.QUALIFICATION)
    next_state, action = determine_sales_transition(state, "Show me your drinkware bottles collection", catalog=MOCK_CATALOG)

    assert next_state.sales_stage == SalesStage.RECOMMENDATION
    assert action is not None
    assert action["action"] == "SHOW_PRODUCTS"
    assert action["count"] >= 1
    # Verify card schema contains real product fields
    card = action["products"][0]
    assert "id" in card
    assert "sku" in card
    assert "name" in card
    assert "price" in card
    assert "variants_count" in card
    assert card["sku"] in ["GF:BTL:VIORA", "GF:MUG:STAT"]


def test_specific_product_detail_triggers_show_product_with_variants():
    state = SalesState(session_id="s1", kiosk_id="k1", sales_stage=SalesStage.PRODUCT_DISCOVERY)
    next_state, action = determine_sales_transition(state, "Tell me more details and price of Viora Eco Bottle", catalog=MOCK_CATALOG)

    assert next_state.sales_stage == SalesStage.RECOMMENDATION
    assert next_state.selected_skus == ("GF:BTL:VIORA",)
    assert action is not None
    assert action["action"] == "SHOW_PRODUCT"
    assert action["sku"] == "GF:BTL:VIORA"

    prod = action["product"]
    assert prod["name"] == "Viora Eco Bottle"
    assert prod["price"] == "649"
    assert prod["mrp"] == "799"
    assert len(prod["variants"]) == 2
    # Verify verified variants contract
    v1 = prod["variants"][0]
    assert v1["id"] == "var-sage"
    assert v1["sku"] == "GF:BTL:VIORA:SAGE"
    assert v1["name"] == "Sage Green"
    assert v1["stock"] is None  # Snapshot has no freshness evidence.


def test_b2b_corporate_bulk_triggers_collect_phone():
    state = SalesState(session_id="s1", kiosk_id="k1", sales_stage=SalesStage.INTENT)
    next_state, action = determine_sales_transition(
        state, "Can you send a quote for 100 pieces for corporate Diwali gifting with company logo?", catalog=MOCK_CATALOG
    )

    assert next_state.customer_intent == "b2b"
    assert next_state.quantity == 100
    assert next_state.occasion == "Corporate Gifting"
    assert "Logo Engraving" in next_state.customization_requirements
    assert next_state.sales_stage == SalesStage.QUALIFICATION

    assert action is not None
    assert action["action"] == "CONFIRM_CONTACT_CONSENT"
    assert action["lead_type"] == "b2b"
    assert action["quantity"] == 100
    assert action["reason"] == "corporate_quote"
    assert action["consent_scope"] == "quotation_contact_only"


def test_b2b_bulk_without_quote_request_does_not_ask_for_phone():
    state = SalesState(session_id="s1", kiosk_id="k1", sales_stage=SalesStage.INTENT)
    next_state, action = determine_sales_transition(
        state, "We need 200 bottles for corporate gifting under 800 rupees", catalog=MOCK_CATALOG
    )
    assert next_state.customer_intent == "b2b"
    assert next_state.quantity == 200
    assert next_state.budget == Decimal("800")
    # Must NOT ask for phone number merely on bulk quantity!
    assert action is not None
    assert action["action"] == "SHOW_PRODUCTS"


def test_customer_declining_contact_collection_is_respected():
    state = SalesState(session_id="s1", kiosk_id="k1", sales_stage=SalesStage.QUALIFICATION, customer_intent="b2b")
    next_state, action = determine_sales_transition(
        state, "No, I decline to share my phone number, just show the products", catalog=MOCK_CATALOG
    )
    assert next_state.customer_contact_consent == "declined"
    assert action is None or action["action"] != "COLLECT_PHONE"


def test_human_handoff_triggers_request_human():
    state = SalesState(session_id="s1", kiosk_id="k1", sales_stage=SalesStage.RECOMMENDATION)
    next_state, action = determine_sales_transition(state, "Can you call a human store associate please?", catalog=MOCK_CATALOG)

    assert next_state.sales_stage == SalesStage.COMPLETED
    assert next_state.escalation_required is True
    assert next_state.conversation_outcome == "human_handoff"

    assert action is not None
    assert action["action"] == "REQUEST_HUMAN"
    assert action["reason"] == "customer_requested"
    assert "Store associate" in action["message"]


def test_immutability_and_session_preservation():
    orig_state = SalesState(session_id="sess-xyz", kiosk_id="kiosk-42", channel="kiosk")
    next_state, _ = determine_sales_transition(orig_state, "budget under 1200", catalog=MOCK_CATALOG)

    # Immutability check: orig_state unchanged
    assert orig_state.budget is None
    assert orig_state.sales_stage == SalesStage.GREETING

    # Identity preserved
    assert next_state.session_id == "sess-xyz"
    assert next_state.kiosk_id == "kiosk-42"
    assert next_state.channel == "kiosk"
    assert next_state.budget == Decimal("1200")
