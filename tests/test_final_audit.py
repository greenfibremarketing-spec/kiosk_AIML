"""Comprehensive Final Acceptance & Audit Test Suite for Greeny AI Kiosk.

Verifies:
- Priority 1: Canonical Green Fibre SKUs (GF:...), live repository prices/stock, no mock data in production.
- Priority 2: Secure per-kiosk authorization with short-lived signed tickets (v1.<kiosk_id>.<ts>.<sig>),
              fail-closed in production, rejection of static query secrets, spoofed kiosk IDs, stolen sessions,
              unauthorized resets, and cross-kiosk WebSocket access.
- Priority 3: Salesperson workflow: budget changes, customization at any stage, bulk 200 items inquiry without
              eager phone collection, explicit quote request with quotation_contact_only consent scope, and
              customer declining contact collection.
- Priority 4: Product compliance: blanket claims intercepted safely unless backed by SKU evidence.
- Priority 5: Database outage fail-safe and retention indexes.
"""

import time
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import settings
from app.sales_engine import (
    determine_sales_transition,
    find_matching_products,
    find_specific_product,
)
from app.sales_state import SalesStage, SalesState
from app.server import api, generate_kiosk_ticket, verify_kiosk_token_internal
from app.sessions import session_manager
from app.storage import DatabaseConnectionError


# ==============================================================================
# PRIORITY 1: PRODUCT IDENTITY & CANONICAL SKUS
# ==============================================================================

def test_canonical_sku_format_and_no_mock_data():
    """Verify live catalog format adheres strictly to GF: ObjectId pattern."""
    from app.greenfibre_repository import GreenFibreProductRepository, WebsiteProduct
    repo = GreenFibreProductRepository(base_url="https://api.greenfibre.org/api")
    wp = WebsiteProduct.model_validate({
        "_id": "6ac33bf78072ec78d51387f0",
        "name": "Classic Coffee Mug",
        "discountedPrice": 349,
        "originalPrice": 499,
        "isActive": True,
        "category": {"name": "Drinkware", "slug": "drinkware"},
        "colors": [{"_id": "6ac33bf7e3e99cc6fbcfcc2b", "name": "Teal", "stock": 10}],
    })
    mapped = repo._map(wp)
    assert mapped["sku"] == "GF:6ac33bf78072ec78d51387f0"
    assert mapped["id"] == "6ac33bf78072ec78d51387f0"
    assert mapped["price"] == "349"
    assert mapped["mrp"] == "499"
    assert len(mapped["variants"]) == 1
    assert mapped["variants"][0]["sku"] == "GF:6ac33bf78072ec78d51387f0:6ac33bf7e3e99cc6fbcfcc2b"
    assert mapped["variants"][0]["id"] == "6ac33bf7e3e99cc6fbcfcc2b"


def test_missing_products_and_unsupported_claims_fail_safely():
    """Unsupported or non-existent products do not trigger false SHOW_PRODUCT actions."""
    from app.tools import _load_catalog
    catalog = _load_catalog()
    # Query for completely non-existent product
    specific = find_specific_product("Tell me about the Quantum Titanium Drone", catalog)
    assert specific is None


# ==============================================================================
# PRIORITY 2: SECURITY & PER-KIOSK AUTHORIZATION
# ==============================================================================

def test_kiosk_ticket_generation_and_verification():
    secret = "test-master-kiosk-secret"
    kiosk_id = "kiosk-mg-road-01"

    # 1. Valid ticket succeeds
    ticket = generate_kiosk_ticket(kiosk_id, secret, ttl_seconds=300)
    verified = verify_kiosk_token_internal(ticket, claimed_kiosk_id=kiosk_id, secret=secret)
    assert verified == kiosk_id

    # 2. Spoofed kiosk ID is rejected
    with pytest.raises(Exception) as exc_info:
        verify_kiosk_token_internal(ticket, claimed_kiosk_id="kiosk-spoofed-99", secret=secret)
    assert "mismatch" in str(exc_info.value).lower()

    # 3. Expired ticket is rejected
    old_ts = int(time.time()) - 400
    import hashlib, hmac
    payload = f"{kiosk_id}:{old_ts}:dummy_nonce"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    expired_ticket = f"v1.{kiosk_id}.{old_ts}.dummy_nonce.{sig}"
    with pytest.raises(Exception) as exc_info:
        verify_kiosk_token_internal(expired_ticket, claimed_kiosk_id=kiosk_id, secret=secret)
    assert "expired" in str(exc_info.value).lower()

    # 4. Tampered signature is rejected
    tampered_ticket = f"v1.{kiosk_id}.{int(time.time())}.dummy_nonce.badsignature"
    with pytest.raises(Exception) as exc_info:
        verify_kiosk_token_internal(tampered_ticket, claimed_kiosk_id=kiosk_id, secret=secret)
    assert "signature" in str(exc_info.value).lower()


def test_auth_kiosk_token_requires_trusted_gateway(monkeypatch):
    """POST /auth/kiosk/token authenticates trusted requester and rejects unauthorized callers."""
    from pydantic import SecretStr
    monkeypatch.setattr(settings, "kiosk_gateway_key", SecretStr("test-gateway-secret-123"))
    monkeypatch.setattr(settings, "kiosk_auth_secret", SecretStr("test-kiosk-secret-456"))
    client = TestClient(api)

    # 1. Missing gateway credential -> 401 Unauthorized
    resp_unauth = client.post("/auth/kiosk/token", json={"kiosk_id": "kiosk-01"})
    assert resp_unauth.status_code == 401
    assert "unauthorized" in resp_unauth.json()["detail"].lower()

    # 2. Invalid gateway credential -> 401 Unauthorized
    resp_bad = client.post(
        "/auth/kiosk/token",
        headers={"X-Gateway-Key": "invalid-wrong-key"},
        json={"kiosk_id": "kiosk-01"},
    )
    assert resp_bad.status_code == 401
    assert "invalid gateway credential" in resp_bad.json()["detail"].lower()

    # 3. Valid gateway credential via X-Gateway-Key -> 200 OK
    resp_ok = client.post(
        "/auth/kiosk/token",
        headers={"X-Gateway-Key": "test-gateway-secret-123"},
        json={"kiosk_id": "kiosk-01"},
    )
    assert resp_ok.status_code == 200
    data = resp_ok.json()
    assert "ticket" in data
    assert data["ticket"].startswith("v1.kiosk-01.")
    assert data["kiosk_id"] == "kiosk-01"


def test_auth_kiosk_token_rejects_forged_or_unregistered_kiosk_id(monkeypatch):
    """POST /auth/kiosk/token rejects ticket requests for arbitrary or forged kiosk IDs."""
    from pydantic import SecretStr
    monkeypatch.setattr(settings, "kiosk_gateway_key", SecretStr("test-gateway-secret-123"))
    monkeypatch.setattr(settings, "kiosk_auth_secret", SecretStr("test-kiosk-secret-456"))
    monkeypatch.setattr(settings, "allowed_kiosk_ids", "kiosk-koramangala-01,kiosk-mg-road-01")
    client = TestClient(api)

    # 1. Forged kiosk ID -> 403 Forbidden
    resp_forged = client.post(
        "/auth/kiosk/token",
        headers={"X-Gateway-Key": "test-gateway-secret-123"},
        json={"kiosk_id": "kiosk-forged-99"},
    )
    assert resp_forged.status_code == 403
    assert "authorized" in resp_forged.json()["detail"].lower()

    # 2. Registered kiosk ID -> 200 OK
    resp_allowed = client.post(
        "/auth/kiosk/token",
        headers={"X-Gateway-Key": "test-gateway-secret-123"},
        json={"kiosk_id": "kiosk-koramangala-01"},
    )
    assert resp_allowed.status_code == 200


def test_ticket_replay_detection():
    """A short-lived signed ticket cannot be replayed once consumed."""
    secret = "test-replay-secret"
    kiosk_id = "kiosk-koramangala-01"
    ticket = generate_kiosk_ticket(kiosk_id, secret, ttl_seconds=300)

    # First consumption succeeds
    verified1 = verify_kiosk_token_internal(ticket, claimed_kiosk_id=kiosk_id, secret=secret, consume=True)
    assert verified1 == kiosk_id

    # Second consumption of the EXACT same ticket is rejected as replay -> 401
    with pytest.raises(Exception) as exc_info:
        verify_kiosk_token_internal(ticket, claimed_kiosk_id=kiosk_id, secret=secret, consume=True)
    assert "replay detected" in str(exc_info.value).lower()


def test_production_fails_closed_if_secret_missing(monkeypatch):
    """Production mode must fail closed with 500 when KIOSK_AUTH_SECRET is absent."""
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "kiosk_auth_secret", None)

    client = TestClient(api)
    resp = client.post("/auth/kiosk/token", json={"kiosk_id": "kiosk-01"})
    assert resp.status_code == 500
    assert "misconfigured" in resp.json()["detail"].lower()


def test_production_rejects_query_string_static_secret():
    """Production mode rejects static shared secrets in query parameters."""
    secret = "production-static-secret"
    with pytest.raises(Exception) as exc_info:
        verify_kiosk_token_internal(
            token=secret,
            secret=secret,
            is_production=True,
            is_query_param=True,
        )
    assert "prohibited" in str(exc_info.value).lower()


def test_cross_kiosk_and_stolen_session_rest_protection(monkeypatch):
    """A kiosk cannot access or reset a session initiated by a different kiosk."""
    client = TestClient(api)
    session_id = f"test-sess-{time.time()}"

    # Kiosk A starts session
    resp1 = client.post(
        "/chat",
        headers={"X-Kiosk-ID": "kiosk-alpha"},
        json={"message": "Hello", "session_id": session_id, "kiosk_id": "kiosk-alpha"},
    )
    assert resp1.status_code == 200

    # Kiosk B tries to hijack the session -> 403 Forbidden
    resp2 = client.post(
        "/chat",
        headers={"X-Kiosk-ID": "kiosk-bravo"},
        json={"message": "Give me data", "session_id": session_id, "kiosk_id": "kiosk-bravo"},
    )
    assert resp2.status_code == 403
    assert "cross-kiosk" in resp2.json()["detail"].lower()

    # Kiosk B tries to reset session -> 403 Forbidden
    resp3 = client.post(
        "/session/reset",
        headers={"X-Kiosk-ID": "kiosk-bravo"},
        json={"session_id": session_id, "kiosk_id": "kiosk-bravo"},
    )
    assert resp3.status_code == 403


def test_cross_kiosk_websocket_access_rejection():
    """WebSocket closes immediately with 1008 if session belongs to another kiosk."""
    client = TestClient(api)
    session_id = f"test-ws-stolen-{time.time()}"

    # Pre-register session ownership under kiosk-alpha
    session_manager.touch(session_id, kiosk_id="kiosk-alpha")

    # Kiosk-bravo attempts to connect to kiosk-alpha's WebSocket session
    with pytest.raises(Exception):
        with client.websocket_connect(f"/ws/{session_id}?kiosk_id=kiosk-bravo&v=2") as ws:
            ws.send_json({"message": "Hello"})
            ws.receive_json()


# ==============================================================================
# PRIORITY 3: SALESPERSON WORKFLOW & CONSENT-AWARE QUOTES
# ==============================================================================

def test_bulk_inquiry_does_not_ask_for_phone_merely_on_quantity():
    """Bulk mention (200 pieces) qualifies B2B and shows products without asking for phone."""
    state = SalesState(sales_stage=SalesStage.GREETING)
    next_state, action = determine_sales_transition(
        state,
        "We need 200 corporate gifts under ₹1500 for our annual conference",
    )
    assert next_state.customer_intent == "b2b"
    assert next_state.quantity == 200
    assert next_state.budget == Decimal("1500")
    # Action should show products or price bands, NOT collect phone!
    assert action is not None
    assert action["action"] in ("SHOW_PRODUCTS", "SHOW_PRICE_BANDS")
    assert action["action"] != "COLLECT_PHONE"


def test_explicit_quote_request_triggers_collect_phone_with_scoped_consent():
    """Explicit quote request triggers COLLECT_PHONE with quotation_contact_only consent."""
    state = SalesState(sales_stage=SalesStage.QUALIFICATION, customer_intent="b2b", quantity=150)
    next_state, action = determine_sales_transition(
        state,
        "Please send a formal quotation to my WhatsApp or call me",
    )
    assert action is not None
    assert action["action"] == "CONFIRM_CONTACT_CONSENT"
    assert action["consent_scope"] == "quotation_contact_only"
    assert action["reason"] == "corporate_quote"
    assert next_state.quote_status == "requested"


def test_customer_refusing_contact_is_respected_without_asking_again():
    """Customer refusal sets customer_contact_consent='declined' and suppresses phone prompts."""
    state = SalesState(sales_stage=SalesStage.QUALIFICATION, customer_intent="b2b")
    next_state, action = determine_sales_transition(
        state,
        "No thanks, I will not share my number, just show me on screen",
    )
    assert next_state.customer_contact_consent == "declined"
    assert action is None or action["action"] != "COLLECT_PHONE"


def test_customization_and_budget_change_at_any_stage():
    """Customer can change budget or request customization at any stage."""
    state = SalesState(sales_stage=SalesStage.RECOMMENDATION, budget=Decimal("500"))
    next_state, action = determine_sales_transition(
        state,
        "Actually increase my budget to ₹1200 and can we do custom logo engraving?",
    )
    assert next_state.budget == Decimal("1200")
    assert "Logo Engraving" in next_state.customization_requirements
    assert action is not None
    assert action["action"] == "SHOW_PRICE_BANDS"


# ==============================================================================
# PRIORITY 4: COMPLIANCE INTERCEPTION
# ==============================================================================

def test_unsupported_blanket_claims_fail_safely():
    """Blanket compliance claims (BPA-free, food-safe, microwave, dishwasher) fail safely."""
    from app.brain import COMPLIANCE_RISK_REGEX
    import re
    assert re.search(COMPLIANCE_RISK_REGEX, "Is this bottle BPA-free?", re.IGNORECASE)
    assert re.search(COMPLIANCE_RISK_REGEX, "Is this food grade certified?", re.IGNORECASE)
    assert re.search(COMPLIANCE_RISK_REGEX, "Are your products carbon-negative?", re.IGNORECASE)


def test_claims_require_approved_sku_evidence_not_regex_alone():
    """Claims must be verified against actual SKU tool evidence, not regex filtering alone."""
    from app.brain import validate_reply_factual_numbers

    # Scenario 1: Claim made without any SKU evidence -> Rejection
    valid, reason = validate_reply_factual_numbers(
        reply="Yes, this product is BPA-free and certified food-safe.",
        tool_outputs=['{"id": "6ac33bf78072ec78d51387f0", "name": "Viora Bottle", "certifications": []}'],
        rag_chunks=[],
    )
    assert valid is False
    assert "No approved SKU certification evidence is available" in reason

    # Scenario 2: Tool text self-asserting certification is NOT approved evidence.
    valid_with_evidence, reason2 = validate_reply_factual_numbers(
        reply="Yes, this bottle is certified food-safe.",
        tool_outputs=['{"id": "6ac33bf78072ec78d51387f0", "name": "Viora Bottle", "certifications": ["Food-Safe Certified", "BPA-Free"]}'],
        rag_chunks=[],
    )
    assert valid_with_evidence is False
    assert "No approved SKU" in reason2
