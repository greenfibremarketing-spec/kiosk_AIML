import pytest
from fastapi.testclient import TestClient
from app.server import api
from app.sales_engine import determine_sales_transition, find_specific_product
from app.sales_state import SalesState, SalesStage
from app.product_repository import JsonProductRepository
from app.guided_contract import ConversationInput
from app.fast_flow import fast_transition, get_ff, drop_ff

MOCK_CATALOG = {
    "products": [
        {
            "sku": "GF:BTL:VIORA",
            "id": "viora-bottle",
            "name": "Viora Eco Bottle",
            "category": "Drinkware",
            "price": 649,
            "mrp": 799,
            "stock": 35,
            "in_stock": True,
            "description": "Sage-green bottle made from upcycled rice-husk biocomposite.",
            "is_active": True,
        },
        {
            "sku": "GF:MUG:STATEMENT",
            "id": "statement-mug",
            "name": "Statement Ceramic-Feel Mug",
            "category": "Drinkware",
            "price": 399,
            "mrp": 499,
            "stock": 50,
            "in_stock": True,
            "description": "Ceramic-like mug made from upcycled rice-husk biocomposite.",
            "is_active": True,
        },
    ]
}


def test_conversation_input_supports_product_name():
    cin = ConversationInput.model_validate({
        "action": "SELECT_PRODUCT",
        "product_name": "Viora Eco Bottle",
        "turn_id": "turn-p1",
    })
    assert cin.product_name == "Viora Eco Bottle"
    assert cin.normalized_message() == "select_product:Viora Eco Bottle"
    cmd = cin.command()
    assert cmd["sku"] == "Viora Eco Bottle"
    assert cmd["product_name"] == "Viora Eco Bottle"


def test_conversation_input_supports_category_name_and_products():
    cin = ConversationInput.model_validate({
        "action": "SELECT_CATEGORY",
        "category_id": "drinkware",
        "category_name": "Drinkware",
        "products": ["Viora Eco Bottle", "Statement Ceramic-Feel Mug"],
        "turn_id": "turn-c1",
    })
    assert cin.category_id == "drinkware"
    assert cin.category_name == "Drinkware"
    assert len(cin.products) == 2


def test_find_specific_product_by_name_and_id():
    p1 = find_specific_product("select_product:GF:BTL:VIORA", MOCK_CATALOG)
    assert p1 is not None and p1["sku"] == "GF:BTL:VIORA"

    p2 = find_specific_product("select_product:viora-bottle", MOCK_CATALOG)
    assert p2 is not None and p2["sku"] == "GF:BTL:VIORA"

    p3 = find_specific_product("select_product:Viora Eco Bottle", MOCK_CATALOG)
    assert p3 is not None and p3["sku"] == "GF:BTL:VIORA"

    p4 = find_specific_product("select_product:Statement Mug", MOCK_CATALOG)
    assert p4 is not None and p4["sku"] == "GF:MUG:STATEMENT"


def test_select_category_lists_products_dynamically():
    state = SalesState(sales_stage=SalesStage.ASK_CATEGORY, customer_name="Rahul", guided_mode=True)
    command = {
        "action": "SELECT_CATEGORY",
        "category_id": "drinkware",
        "turn_id": "turn-c2",
    }
    next_state, action = determine_sales_transition(
        state,
        user_message="select_category:drinkware",
        catalog=MOCK_CATALOG,
        command=command,
    )
    assert action["action"] == "SHOW_PRODUCTS"
    assert action["count"] == 2
    # Confirms dynamic product naming in avatar message
    assert "Rahul" in action["message"]
    assert "Drinkware" in action["message"]
    assert "Viora Eco Bottle" in action["message"]
    assert "Statement Ceramic-Feel Mug" in action["message"]


def test_determine_sales_transition_select_product_with_great_choice():
    state = SalesState(sales_stage=SalesStage.RECOMMENDATION, customer_name="Rahul", guided_mode=True)
    command = {
        "action": "SELECT_PRODUCT",
        "product_name": "Viora Eco Bottle",
        "turn_id": "turn-p3",
    }
    next_state, action = determine_sales_transition(
        state,
        user_message="select_product:Viora Eco Bottle",
        catalog=MOCK_CATALOG,
        command=command,
    )
    assert action["action"] == "SHOW_PRODUCT"
    assert action["sku"] == "GF:BTL:VIORA"
    assert "Great choice, Rahul!" in action["message"]
    assert "Viora Eco Bottle" in action["message"]
    assert "₹649" in action["message"]
    assert next_state.sales_stage == SalesStage.PRODUCT_DETAIL
    assert next_state.selected_skus == ("GF:BTL:VIORA",)


def test_fast_flow_category_then_product_with_great_choice():
    sid = "test-ff-full-flow"
    drop_ff(sid)
    ctx = get_ff(sid)
    ctx.name = "Aarav"
    ctx.stage = "CATEGORY"
    ctx.products = list(MOCK_CATALOG["products"])

    # Step 1: User selects category
    reply_cat, screen_cat, _ = fast_transition(sid, "select_category:drinkware")
    assert screen_cat["action"] == "SHOW_PRODUCTS"
    assert "Aarav" in reply_cat
    assert "Drinkware" in reply_cat
    assert "Viora Eco Bottle" in reply_cat
    assert ctx.stage == "CATEGORY"

    # Step 2: User selects product dynamically
    reply_prod, screen_prod, _ = fast_transition(sid, "select_product:statement-mug")
    assert screen_prod["action"] == "SHOW_PRODUCT"
    assert screen_prod["product"]["sku"] == "GF:MUG:STATEMENT"
    assert "Great choice, Aarav!" in reply_prod
    assert "Statement Ceramic-Feel Mug" in reply_prod
    assert ctx.stage == "PRODUCT"
    drop_ff(sid)
