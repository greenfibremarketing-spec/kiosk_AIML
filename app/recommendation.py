"""Cart recommendation feature: additive module for Greenfibre kiosk.

Provides REQUEST_RECOMMENDATION / ACCEPT_RECOMMENDATION / DECLINE_RECOMMENDATION
client actions and a SHOW_RECOMMENDATION server action.

Never modifies existing stages, validation, or CONFIRM_BUY behaviour.
All discount arithmetic is centralised in RECOMMENDATION_DISCOUNT_PERCENT.
"""

from __future__ import annotations
import uuid
from typing import Any, Dict, Optional

from app.product_repository import product_view, valid_price
from app.tools import get_product_repository

import logging
logger = logging.getLogger("greenie.recommendation")

# ── Single constant ──────────────────────────────────────────────────────────
RECOMMENDATION_DISCOUNT_PERCENT = 10  # Set this to change the bundle discount

# ── Deterministic pairing map (base_sku -> suggested_sku) ────────────────────
# Format: {sku_prefix_or_exact: suggested_sku}
_PAIRING_MAP: Dict[str, str] = {
    # These will be tried in order; first prefix match wins
    # Fill in real SKU pairs once catalog is stable
    "GF:MUG":    "GF:COASTER",
    "GF:PLATE":  "GF:BOWL",
    "GF:BOWL":   "GF:PLATE",
    "GF:CUP":    "GF:SAUCER",
    "GF:BOTTLE": "GF:CARRY-BAG",
    "GF:TRAY":   "GF:MUG",
}


def _find_suggested_sku(base_sku: str, products: list) -> Optional[str]:
    """Return a suggested SKU using the pairing map, falling back to the next product."""
    if not base_sku:
        return None
    base_upper = base_sku.upper()
    for pattern, suggested in _PAIRING_MAP.items():
        if base_upper.startswith(pattern) or base_upper == pattern:
            return suggested
    # Fallback: pick next product in catalog that is different from base
    for p in products:
        sku = (p.get("sku") or "").strip()
        if sku and sku.upper() != base_upper:
            return sku
    return None


def _product_card(p: Dict[str, Any]) -> Dict[str, Any]:
    """Return a product card matching the shape expected by the frontend."""
    view = product_view(p) if p else {}
    merged = {**p, **view}
    images = merged.get("images") or []
    image = (images[0] if images else None) or merged.get("image") or ""
    try:
        price = float(merged.get("price") or merged.get("salePrice") or 0)
    except Exception:
        price = 0
    return {
        "sku": merged.get("sku") or "",
        "id": merged.get("id") or merged.get("sku") or "",
        "name": merged.get("name") or "",
        "price": price,
        "salePrice": price,
        "image": image,
        "images": images,
        "category": merged.get("category") or "",
        "description": merged.get("description") or merged.get("blurb") or "",
    }


def build_recommendation_action(
    base_sku: str,
    name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Build a SHOW_RECOMMENDATION action payload.
    Returns None if no suggestion can be made.
    """
    try:
        repo = get_product_repository()
        all_products = list(repo.all()) if hasattr(repo, "all") else []
        if not all_products:
            from app.tools import _load_catalog
            catalog = _load_catalog()
            all_products = catalog.get("products", [])
    except Exception as exc:
        logger.warning("[Reco] Could not load products: %s", exc)
        return None

    # Find base product
    base_product = None
    for p in all_products:
        if (p.get("sku") or "").strip().upper() == base_sku.upper():
            base_product = p
            break
    if not base_product:
        for p in all_products:
            if base_sku.upper() in (p.get("sku") or "").upper():
                base_product = p
                break
    if not base_product and all_products:
        base_product = all_products[0]

    suggested_sku = _find_suggested_sku(base_sku, all_products)
    if not suggested_sku:
        logger.info("[Reco] No suggestion available for sku=%s", base_sku)
        return None

    # Find suggested product
    suggested_product = None
    for p in all_products:
        if (p.get("sku") or "").strip().upper() == suggested_sku.upper():
            suggested_product = p
            break
    if not suggested_product:
        for p in all_products:
            if suggested_sku.upper() in (p.get("sku") or "").upper():
                suggested_product = p
                break
    if not suggested_product and len(all_products) > 1:
        for p in all_products:
            if (p.get("sku") or "") != (base_product.get("sku") or ""):
                suggested_product = p
                break

    if not suggested_product:
        logger.info("[Reco] Could not find suggested product for sku=%s", suggested_sku)
        return None

    try:
        base_price = float(base_product.get("price") or base_product.get("salePrice") or 0)
    except Exception:
        base_price = 0
    try:
        sugg_price = float(suggested_product.get("price") or suggested_product.get("salePrice") or 0)
    except Exception:
        sugg_price = 0

    individual_total = base_price + sugg_price
    discount_fraction = RECOMMENDATION_DISCOUNT_PERCENT / 100
    bundle_price = round(individual_total * (1 - discount_fraction))
    saved_amount = round(individual_total - bundle_price)

    base_name = base_product.get("name", "this product")
    sugg_name = suggested_product.get("name", "a companion")

    offer_message = (
        f"Here is a lovely match for your {base_name}. "
        f"Buy {base_name} and {sugg_name} together for {int(bundle_price)} rupees, "
        f"and enjoy {RECOMMENDATION_DISCOUNT_PERCENT} percent off the total."
    )

    speech_id = uuid.uuid4().hex

    return {
        "action": "SHOW_RECOMMENDATION",
        "message": offer_message,
        "base_product": _product_card(base_product),
        "suggested_product": _product_card(suggested_product),
        "individual_total": individual_total,
        "bundle_price": bundle_price,
        "discount_percent": RECOMMENDATION_DISCOUNT_PERCENT,
        "saved_amount": saved_amount,
        "speech_id": speech_id,
        "awaiting_speech_done": False,
    }


def handle_accept_recommendation(
    base_sku: str,
    suggested_sku: str,
    name: Optional[str] = None,
) -> Dict[str, Any]:
    """Accept the bundle: return updated cart info message."""
    try:
        repo = get_product_repository()
        all_products = list(repo.all()) if hasattr(repo, "all") else []
        if not all_products:
            from app.tools import _load_catalog
            catalog = _load_catalog()
            all_products = catalog.get("products", [])
    except Exception:
        all_products = []

    total = 0.0
    for p in all_products:
        sku = (p.get("sku") or "").upper()
        if sku == base_sku.upper() or sku == suggested_sku.upper():
            try:
                total += float(p.get("price") or p.get("salePrice") or 0)
            except Exception:
                pass

    discount_fraction = RECOMMENDATION_DISCOUNT_PERCENT / 100
    bundle_total = round(total * (1 - discount_fraction))
    name_part = f", {name}" if name else ""
    msg = f"Wonderful choice! I have added it to your cart{name_part}. Your new total is {int(bundle_total)} rupees."
    logger.info("[Reco] accepted base=%s suggested=%s total=%s", base_sku, suggested_sku, bundle_total)
    return {
        "action": "CONFIRM_BUY",
        "message": msg,
        "bundle_accepted": True,
        "new_total": bundle_total,
    }


def handle_decline_recommendation(name: Optional[str] = None) -> Dict[str, Any]:
    """Decline: just reassure the customer."""
    logger.info("[Reco] declined")
    return {
        "action": "DECLINE_RECOMMENDATION",
        "message": "No problem at all. Let me know if you need anything else.",
    }
