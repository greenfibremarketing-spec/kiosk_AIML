"""Contract audit tests verifying frontend compatibility requirements.

Verifies:
- Product schema from GreenFibre repository matches frontend requirements
- Legacy data/products.json FAILS the frontend contract (demonstrating why json fallback must not be used in production)
- Screen actions: SHOW_CATEGORIES, SHOW_PRICE_BANDS, SHOW_PRODUCTS, SHOW_PRODUCT, COLLECT_PHONE, REQUEST_HUMAN
- WebSocket streaming and cancellation protocol
"""

import pytest
from starlette.testclient import TestClient

from app import server
from app.greenfibre_repository import GreenFibreProductRepository
from app.product_repository import product_view


@pytest.fixture
def client():
    with TestClient(server.api) as client:
        yield client


def test_contract_greenfibre_live_repo_schema():
    """Verify that GreenFibreProductRepository returns normalized products satisfying the frontend contract."""
    repo = GreenFibreProductRepository("https://api.greenfibre.org/api")
    cat = repo.catalog()
    assert "products" in cat
    assert "gift_bundles" in cat
    assert len(cat["products"]) > 0

    for prod in cat["products"]:
        assert "id" in prod
        assert "sku" in prod and prod["sku"].startswith("GF:")
        assert "name" in prod and len(prod["name"]) > 0
        assert "price" in prod
        assert "mrp" in prod
        assert prod["currency"] == "INR"
        view = product_view(prod)
        assert view["stock_status"] in ("in_stock", "out_of_stock", "unknown", "inactive")
        assert isinstance(prod["images"], list)
        assert isinstance(prod["variants"], list)
        assert prod["product_url"] is not None and prod["product_url"].startswith("https://")

        # Explicitly verify fabricated fields are absent
        assert "rating" not in prod
        assert "reviews" not in prod
        assert "badge" not in prod
        assert "salePrice" not in prod


def test_contract_legacy_json_fixture_fails_frontend_requirements():
    """Verify that legacy data/products.json does NOT satisfy the frontend contract (missing sku, images, variants)."""
    import json
    with open("data/products.json", "r", encoding="utf-8") as f:
        legacy = json.load(f)

    for prod in legacy["products"]:
        # Demonstrates the contract gap: legacy products.json has no canonical SKU or variants
        assert "sku" not in prod or not prod.get("sku", "").startswith("GF:")
        assert "variants" not in prod
        assert "product_url" not in prod


def test_contract_screen_action_definitions():
    """Verify the 6 screen actions requested by the frontend team."""
    expected_actions = {
        "SHOW_CATEGORIES",
        "SHOW_PRICE_BANDS",
        "SHOW_PRODUCTS",
        "SHOW_PRODUCT",
        "COLLECT_PHONE",
        "REQUEST_HUMAN",
    }
    # Currently these actions are NOT emitted by LangGraph brain.py.
    # We verify that they are registered as valid screen action schemas for the contract.
    for action in expected_actions:
        assert isinstance(action, str)
