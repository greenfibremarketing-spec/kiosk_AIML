"""Cart recommendation feature: additive module for Greenfibre kiosk.

Provides REQUEST_RECOMMENDATION / ACCEPT_RECOMMENDATION / DECLINE_RECOMMENDATION
client actions and a SHOW_RECOMMENDATION server action.

Never modifies existing stages, validation, or CONFIRM_BUY behaviour.
All discount arithmetic is centralised in RECOMMENDATION_DISCOUNT_PERCENT.
"""

from __future__ import annotations
import re
import uuid
from typing import Any, Dict, List, Optional

from app.product_repository import is_active, product_view, valid_price
from app.tools import get_product_repository

import logging
logger = logging.getLogger("greenie.recommendation")

# ── Single constants ──────────────────────────────────────────────────────────
RECOMMENDATION_DISCOUNT_PERCENT = 10  # Set this to change the bundle discount
PAIRUP_DISCOUNT_PERCENT = 5          # 5% extra discount for pair-up additions

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
        raw_price = float(merged.get("price") or 0)
        raw_mrp = float(merged.get("mrp") or 0)
        if raw_price > 0 and raw_mrp > 0 and abs(raw_mrp / raw_price - 1.35) < 0.05:
            base_mrp = raw_price
        else:
            base_mrp = raw_price or raw_mrp or float(merged.get("salePrice") or 0)
    except Exception:
        base_mrp = 0
    sale_price = round(base_mrp * 0.90) if base_mrp > 0 else 0
    return {
        "sku": merged.get("sku") or "",
        "id": merged.get("id") or merged.get("sku") or "",
        "name": merged.get("name") or "",
        "price": base_mrp,
        "mrp": base_mrp,
        "salePrice": sale_price,
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


def _build_info_text(p: Dict[str, Any]) -> str:
    """Build a short, single-sentence info string of max 80 chars from existing product data."""
    raw = (
        p.get("shortDescription")
        or p.get("blurb")
        or p.get("description")
        or p.get("tagline")
        or p.get("sustainability_notes")
        or ""
    )
    clean = re.sub(r"<[^>]+>", " ", str(raw)).strip()
    clean = re.sub(r"\s+", " ", clean)
    if not clean:
        cat = str(p.get("category") or "eco essential").strip()
        clean = f"Sustainable {cat} made from 100% upcycled rice husk."
    first_sent = re.split(r"(?<=[.!?])\s+", clean)[0].strip()
    if not first_sent:
        first_sent = clean
    if len(first_sent) > 80:
        truncated = first_sent[:77].rsplit(" ", 1)[0].rstrip(",;:- ") + "..."
        if len(truncated) > 80:
            truncated = first_sent[:77] + "..."
        return truncated
    return first_sent


def _rank_products_by_similarity(
    primary_product: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    prioritize_pairing_map: bool = False,
) -> List[Dict[str, Any]]:
    """Rank candidate products using embeddings/similarity or pairing map heuristics."""
    if not candidates:
        return []

    pairing_boost: Dict[str, float] = {}
    if prioritize_pairing_map:
        base_sku = (primary_product.get("sku") or "").upper()
        suggested = _find_suggested_sku(base_sku, candidates)
        if suggested:
            pairing_boost[suggested.upper()] = 100.0

    embeddings_scored = False
    scores: List[float] = [0.0] * len(candidates)
    try:
        from app import rag
        if getattr(rag, "_embeddings_instance", None) is not None:
            emb = rag._embeddings_instance
            p_text = f"{primary_product.get('name', '')} {primary_product.get('category', '')} {primary_product.get('description', '')}"
            c_texts = [f"{c.get('name', '')} {c.get('category', '')} {c.get('description', '')}" for c in candidates]
            p_vec = emb.embed_query(p_text)
            c_vecs = emb.embed_documents(c_texts)
            import numpy as np
            p_norm = np.linalg.norm(p_vec)
            for i, cv in enumerate(c_vecs):
                denom = p_norm * np.linalg.norm(cv)
                sim = float(np.dot(p_vec, cv) / denom) if denom > 0 else 0.0
                scores[i] = sim
            embeddings_scored = True
    except Exception as exc:
        logger.debug("[Pairups] Embedding ranking skipped: %s", exc)

    if not embeddings_scored:
        p_tokens = set(re.findall(r"\w+", f"{primary_product.get('name', '')} {primary_product.get('description', '')}".lower()))
        for i, c in enumerate(candidates):
            c_tokens = set(re.findall(r"\w+", f"{c.get('name', '')} {c.get('description', '')}".lower()))
            union = p_tokens | c_tokens
            scores[i] = len(p_tokens & c_tokens) / len(union) if union else 0.0

    for i, c in enumerate(candidates):
        sku = (c.get("sku") or "").upper()
        if sku in pairing_boost:
            scores[i] += pairing_boost[sku]

    indexed = sorted(range(len(candidates)), key=lambda idx: scores[idx], reverse=True)
    return [candidates[i] for i in indexed]


def get_pairup_recommendations(
    product_id: str,
    cart: Optional[str] = "",
) -> List[Dict[str, Any]]:
    """
    Select 3 to 4 pair-up products for the given product_id.
    Excludes primary product, anything in cart, and out-of-stock items.
    Mixes up to 2 similar products (same category) and up to 2 complementary
    products from a different category.
    """
    try:
        from app.tools import _load_catalog
        catalog = _load_catalog()
        all_products = catalog.get("products", [])
    except Exception as exc:
        logger.warning("[Pairups] Could not load products: %s", exc)
        return []

    if not all_products:
        return []

    # 1. Identify primary product
    primary_product = None
    pid_clean = str(product_id or "").strip().lower()
    for p in all_products:
        p_id = str(p.get("id") or "").strip().lower()
        p_sku = str(p.get("sku") or "").strip().lower()
        if pid_clean and (p_id == pid_clean or p_sku == pid_clean or pid_clean in p_sku or pid_clean in p_id):
            primary_product = p
            break

    # Build excluded set
    excluded = set()
    if pid_clean:
        excluded.add(pid_clean)
    if primary_product:
        excluded.add(str(primary_product.get("id") or "").strip().lower())
        excluded.add(str(primary_product.get("sku") or "").strip().lower())

    if cart:
        for cid in str(cart).split(","):
            c_clean = cid.strip().lower()
            if c_clean:
                excluded.add(c_clean)

    # 2. Filter available in-stock products
    available = []
    for p in all_products:
        p_id = str(p.get("id") or "").strip().lower()
        p_sku = str(p.get("sku") or "").strip().lower()
        if p_id in excluded or p_sku in excluded:
            continue
        if not is_active(p):
            continue
        stock_status = str(p.get("stock_status") or "").lower()
        if stock_status == "out_of_stock":
            continue
        if p.get("in_stock") is False:
            continue
        stock_qty = p.get("stockQuantity") or p.get("stock")
        if stock_qty is not None:
            try:
                if int(stock_qty) <= 0:
                    continue
            except Exception:
                pass
        available.append(p)

    if not available:
        return []

    if primary_product is None:
        primary_product = available[0]

    primary_cat = str(primary_product.get("category") or "").strip().lower()
    same_cat = [p for p in available if str(p.get("category") or "").strip().lower() == primary_cat]
    diff_cat = [p for p in available if str(p.get("category") or "").strip().lower() != primary_cat]

    # Rank products
    similar_ranked = _rank_products_by_similarity(primary_product, same_cat)
    comp_ranked = _rank_products_by_similarity(primary_product, diff_cat, prioritize_pairing_map=True)

    # Mix: up to 2 similar and up to 2 complementary
    picked: List[Dict[str, Any]] = []
    picked.extend(similar_ranked[:2])
    picked.extend(comp_ranked[:2])

    # If fewer than 3, backfill from remaining
    if len(picked) < 3:
        seen_skus = {p.get("sku") for p in picked}
        for p in comp_ranked + similar_ranked:
            if p.get("sku") not in seen_skus:
                picked.append(p)
                seen_skus.add(p.get("sku"))
                if len(picked) >= 4:
                    break

    # Maximum 4 products, never more
    picked = picked[:4]

    results = []
    for p in picked:
        try:
            raw_price = float(p.get("price") or 0)
            raw_mrp = float(p.get("mrp") or 0)
            if raw_price > 0 and raw_mrp > 0 and abs(raw_mrp / raw_price - 1.35) < 0.05:
                base_mrp = raw_price
            else:
                base_mrp = raw_price or raw_mrp or float(p.get("salePrice") or 0)
        except Exception:
            base_mrp = 0.0

        sale_price = round(base_mrp * 0.90) if base_mrp > 0 else 0.0

        images = p.get("images") or []
        image = (images[0] if images else None) or p.get("image") or ""

        results.append({
            "id": str(p.get("id") or p.get("sku") or ""),
            "sku": str(p.get("sku") or p.get("id") or ""),
            "name": str(p.get("name") or ""),
            "price": base_mrp,
            "mrp": base_mrp,
            "salePrice": sale_price,
            "image": image,
            "info": _build_info_text(p),
        })

    return results
