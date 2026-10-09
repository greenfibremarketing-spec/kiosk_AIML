"""Deterministic Sales Transition Engine for Greeny AI Kiosk.

Transitions the conversation through structured sales stages:
GREETING -> INTENT -> QUALIFICATION -> PRODUCT_DISCOVERY -> RECOMMENDATION -> OBJECTION_HANDLING -> CONVERSION -> COMPLETED.

Emits structured UI actions with currently valid Green Fibre product IDs and variant IDs:
- SHOW_CATEGORIES
- SHOW_PRICE_BANDS
- SHOW_PRODUCTS
- SHOW_PRODUCT
- COLLECT_PHONE
- REQUEST_HUMAN
"""

from decimal import Decimal
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from app.sales_state import SalesStage, SalesState
from app.tools import _load_catalog, get_product_repository
from app.product_repository import product_view, is_active, valid_price

logger = logging.getLogger("greenie.sales_engine")

# Standard kiosk categories
CATEGORIES_METADATA = [
    {"id": "drinkware", "name": "Drinkware & Bottles", "icon": "bottle"},
    {"id": "kitchen_dining", "name": "Kitchen & Dining", "icon": "utensils"},
    {"id": "office_desk", "name": "Office & Desk", "icon": "briefcase"},
    {"id": "gift_bundles", "name": "Gift Sets & Bundles", "icon": "gift"},
    {"id": "corporate_b2b", "name": "Corporate & Bulk Gifting", "icon": "building"},
]

PRICE_BANDS_METADATA = [
    {"id": "under_500", "label": "Under ₹500", "min": 0, "max": 500},
    {"id": "500_1000", "label": "₹500 - ₹1,000", "min": 500, "max": 1000},
    {"id": "1000_1500", "label": "₹1,000 - ₹1,500", "min": 1000, "max": 1500},
    {"id": "above_1500", "label": "Above ₹1,500", "min": 1500, "max": None},
]


def extract_customer_name(text: str) -> Optional[str]:
    """Extract customer name from introductory phrases."""
    patterns = [
        r"(?:my name is|i am|i'm|call me|myself)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        r"(?:naam hai|mera naam)\s+([A-Z][a-z]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw_match = m.group(1).strip()
            # Split out connecting words like "and", "who", "from", "for"
            parts = re.split(r"\s+(?:and|who|from|here|want|need|looking|interested|for)\b", raw_match, flags=re.IGNORECASE)
            name = parts[0].strip()
            name_words = set(name.lower().split())
            stop_words = {"looking", "searching", "for", "here", "greeny", "interested", "shopping", "buying", "corporate", "a", "an", "the", "just"}
            if name and not (name_words & stop_words):
                return name.title()
    return None


def extract_budget(text: str) -> Optional[Decimal]:
    """Extract numeric budget in INR from user query."""
    cleaned = re.sub(r"(?<=\d),(?=\d)", "", text)
    patterns = [
        r"(?:under|below|within|upto|up to|budget(?: of| is| was)?|max|approx|rs\.?|inr|₹)\s*(\d{2,6})(?!\s*(?:units|pcs|pieces|items|bottles|sets|attendees|people))",
        r"(\d{2,6})\s*(?:rupees|rs|inr|tak)",
    ]
    for pat in patterns:
        matches = list(re.finditer(pat, cleaned, re.IGNORECASE))
        if matches:
            for m in reversed(matches):
                try:
                    val = Decimal(m.group(1))
                    if 50 <= val <= 500000:
                        return val
                except Exception:
                    pass
    return None


def extract_quantity(text: str) -> Optional[int]:
    """Extract bulk quantity for B2B qualification."""
    cleaned = re.sub(r"(?<=\d),(?=\d)", "", text)
    patterns = [
        r"(?:around|approx|need|order|buy|want)?\s*(\d{2,5})\s*(?:pieces|pcs|units|items|bottles|sets|gifts|numbers|attendees)",
        r"(?:need|order|buy|want)\s*(\d{2,5})",
    ]
    for pat in patterns:
        m = re.search(pat, cleaned, re.IGNORECASE)
        if m:
            try:
                qty = int(m.group(1))
                if qty >= 10:
                    return qty
            except Exception:
                pass
    return None


def find_matching_products(
    query: str,
    catalog: Dict[str, Any],
    max_items: int = 4,
    max_budget: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Find real product cards from the authoritative catalogue, optionally filtered by budget."""
    products = [p for p in catalog.get("products", []) if p.get("sku") and is_active(p)]
    if not products:
        return []

    q_lower = query.lower()
    matches = []

    # Check for direct SKU, name, category, or budget match
    for p in products:
        price_val = None
        try:
            if p.get("price") is not None:
                price_val = float(p.get("price"))
        except Exception:
            pass

        if not p.get("sku") or not is_active(p):
            continue
        if max_budget is not None and (price_val is None or not valid_price(price_val) or price_val > max_budget):
            continue

        name = p.get("name", "").lower()
        cat = p.get("category", "").lower()
        desc = p.get("description", "").lower()
        sku = p.get("sku", "").lower()

        score = 0
        if q_lower in name:
            score += 3
        if any(term in name for term in q_lower.split() if len(term) > 3):
            score += 2
        if any(term in cat or term in name for term in ["bottle", "drinkware", "cup", "mug", "tumbler", "bowl", "bundle", "kitchen", "dining"] if term in q_lower):
            score += 2

        if score > 0 or max_budget is not None:
            matches.append((score, p))

    matches.sort(key=lambda x: x[0], reverse=True)
    results = [m[1] for m in matches[:max_items]]

    # Fallback to products within budget if no specific term match
    if not results and products:
        if max_budget is not None:
            results = [
                p for p in products
                if p.get("price") is not None and float(p.get("price", 999999)) <= max_budget
            ][:max_items]
        else:
            results = products[:max_items]

    return results


def find_specific_product(query: str, catalog: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find a single specific product when user asks for product details."""
    products = [p for p in catalog.get("products", []) if p.get("sku") and is_active(p)]
    q_lower = query.lower()

    for p in products:
        name = p.get("name", "").lower()
        sku = p.get("sku", "").lower()
        pid = p.get("id", "").lower()
        website_sku = (p.get("website_sku") or "").lower()

        for variant in p.get('variants', []):
            if variant.get('sku') and variant['sku'].lower() in q_lower:
                return {**p, 'sku': variant['sku'], 'variant_id': variant.get('id'),
                        'variant_name': variant.get('name'), 'stock': variant.get('stock'),
                        'in_stock': variant['stock'] > 0 if type(variant.get('stock')) is int else None}
        # 1. Exact canonical SKU or ID match
        if (sku and sku in q_lower) or (pid and pid in q_lower) or (website_sku and website_sku in q_lower):
            return p

        # 2. Substantial name match
        if len(name) > 4 and name in q_lower:
            return p

    # 3. Query specifies distinctive words of product name (e.g., "Statement Mug")
    stop_words = {"tell", "details", "about", "product", "this", "that", "show", "give", "please", "have", "with", "from", "want", "like", "need", "your", "what", "know", "more", "item"}
    query_tokens = [w for w in re.findall(r'[a-z0-9]+', q_lower) if len(w) >= 3 and w not in stop_words]
    if query_tokens:
        for p in products:
            name = p.get("name", "").lower()
            if all(w in name for w in query_tokens):
                return p

    # 4. Model keyword matching with disambiguation
    for p in products:
        name = p.get("name", "").lower()
        for word in ["viora", "statement", "classic mug", "eco bottle", "copper", "tumbler", "bowl", "coffee mug", "cutlery"]:
            if word in q_lower and word in name:
                # Disambiguate volume if specified
                if "900" in q_lower and "900" in name:
                    return p
                if "400" in q_lower and "400" in name:
                    return p
                return p
    return None


def determine_sales_transition(
    current_state: SalesState,
    user_message: str,
    tool_outputs: Optional[List[str]] = None,
    catalog: Optional[Dict[str, Any]] = None,
) -> Tuple[SalesState, Optional[Dict[str, Any]]]:
    """Compute the deterministic next SalesState and optional structured UI action.

    Guarantees:
    - Never mutates session identity (session_id, kiosk_id, channel).
    - Preserves previous customer preferences and customer_name.
    - Emits real, verified Green Fibre SKUs and variant IDs.
    - Handles human handoff, B2B qualification, budget, categories, and products.
    """
    msg = user_message.strip().lower()
    if catalog is None:
        try:
            repository = get_product_repository()
            snapshot = repository.catalog()
            verified = []
            for candidate in snapshot.get("products", []):
                if not candidate.get("sku"):
                    continue
                fresh = repository.get(candidate["sku"])
                if fresh and is_active(fresh):
                    view = product_view(fresh)
                    if fresh.get("source") == "greenfibre_api" and view["stock_status"] != "in_stock":
                        continue
                    verified.append({**fresh, **view})
            catalog = {"products": verified}
        except Exception:
            catalog = {"products": []}

    catalog = {**catalog, "products": [
        {**p, **product_view(p)} for p in catalog.get("products", [])
        if p.get("sku") and is_active(p)
    ]}
    updates: Dict[str, Any] = {}
    action: Optional[Dict[str, Any]] = None

    refusal_keywords = [
        "don't ask", "dont ask", "no phone", "no number", "decline", "not sharing",
        "not now", "no contact", "do not consent", "don't consent", "nahi", "don't want to share", "dont want to share",
        "skip phone", "refuse", "will not share", "won't share", "not share", "not giving", "cannot share"
    ]
    if any(k in msg for k in refusal_keywords) or (current_state.contact_consent_pending and msg.rstrip(".! ") in ("no", "no thanks", "nahin", "nahi", "नहीं")):
        updates["customer_contact_consent"] = "declined"
    if any(k in msg for k in ["revoke", "withdraw consent", "stop contacting", "do not contact", "don't contact"]):
        updates["customer_contact_consent"] = "revoked"
    if "customer_contact_consent" in updates:
        updates["lead_status"] = "declined"
        updates["contact_consent_pending"] = False
        return current_state.with_updates(**updates), None
    if (current_state.contact_consent_pending and msg.rstrip(".! ") in ("yes", "yes please")) or msg.rstrip(".! ") in ("i consent to quotation contact", "yes, contact me about this quote", "i agree to quotation contact"):
        updates["customer_contact_consent"] = "granted"
        updates["contact_consent_pending"] = False
        return current_state.with_updates(**updates), {
            "action": "COLLECT_PHONE", "consent_scope": "quotation_contact_only",
            "submission_enabled": False,
        }


    # ── 1. Check for Human Handoff Request ────────────────────────────────────
    human_phrases = [
        "human", "store manager", "sales manager", "store associate", "store staff",
        "talk to someone", "speak to someone", "call someone", "connect with someone",
        "connect with me", "reach out to me", "store executive", "sales executive",
        "human agent", "talk to human", "speak with human"
    ]
    if any(p in msg for p in human_phrases):
        updates["sales_stage"] = SalesStage.COMPLETED
        updates["escalation_required"] = True
        updates["conversation_outcome"] = "human_handoff"
        action = {
            "action": "REQUEST_HUMAN",
            "reason": "customer_requested",
            "message": "Store associate requested by customer.",
        }
        return current_state.with_updates(**updates), action

    # ── 2. Check for Customer Contact Refusal / Consent Decline ───────────────
    # ── 3. Check for Name Sharing ─────────────────────────────────────────────
    # Customer name is retained for operational CRM storage rather than anonymous SalesState
    name = extract_customer_name(user_message)

    # ── 4. Check for Customization Requests (at any stage) ────────────────────
    if any(k in msg for k in ["custom logo", "logo", "engrav", "branding", "personalized", "print"]):
        customizations = set(current_state.customization_requirements)
        customizations.add("Logo Engraving")
        updates["customization_requirements"] = tuple(customizations)

    # ── 5. Check for B2B Bulk Qualification ───────────────────────────────────
    b2b_keywords = ["corporate", "b2b", "bulk", "wholesale", "company", "employee", "diwali gift", "diwali gifts", "clients", "offices", "bulk order"]
    qty = extract_quantity(user_message)
    is_b2b = any(k in msg for k in b2b_keywords) or (qty is not None and qty >= 20)

    # Explicit quote request or phone contact permission
    quote_callback_phrases = [
        "send quote", "send a quote", "share quote", "call me", "contact me",
        "take my number", "share on whatsapp", "email quote", "send quotation",
        "give quote", "give me a quote", "quotation callback", "send me quote",
        "call with pricing", "reach out", "quotation", "quote", "whatsapp",
        "prepare quote", "prepare a quote", "prepare quotation", "prepare a quotation",
        "formal quote", "formal quotation", "prepare a formal"
    ]
    phone_pattern = r'(?:\+91[\s-]?)?[6-9](?:[\s-]?\d){9}\b'
    has_explicit_quote_request = any(k in msg for k in quote_callback_phrases)

    budget = extract_budget(user_message)
    if budget:
        updates["budget"] = budget

    if is_b2b or current_state.customer_intent == "b2b":
        updates["customer_intent"] = "b2b"
        if qty:
            updates["quantity"] = qty
        if any(term in msg for term in ["diwali", "annual", "conference", "new year", "summit", "gifting"]):
            updates["occasion"] = "Corporate Gifting"

    # Track product category preferences
    if any(k in msg for k in ["bottle", "drinkware", "flask", "tumbler"]):
        prefs = set(current_state.product_preferences)
        prefs.add("Drinkware & Bottles")
        updates["product_preferences"] = tuple(prefs)

    if is_b2b or current_state.customer_intent == "b2b":
        if has_explicit_quote_request and current_state.customer_contact_consent not in ("declined", "revoked"):
            updates["sales_stage"] = SalesStage.QUALIFICATION
            updates["lead_status"] = "qualifying"
            updates["quote_status"] = "requested"
            updates["contact_consent_pending"] = current_state.customer_contact_consent != "granted"
            action = {
                "action": "CONFIRM_CONTACT_CONSENT" if current_state.customer_contact_consent != "granted" else "COLLECT_PHONE",
                "submission_enabled": False,
                "reason": "corporate_quote",
                "consent_scope": "quotation_contact_only",
                "lead_type": "b2b",
                "quantity": qty or current_state.quantity,
            }
            return current_state.with_updates(**updates), action

        # If B2B inquiry includes budget or product request, show matching products
        if budget or any(k in msg for k in ["gift", "bottle", "product", "recommend", "show me", "options", "what do you have"]):
            updates["sales_stage"] = SalesStage.RECOMMENDATION
            matching = find_matching_products(user_message, catalog, max_items=4, max_budget=float(budget) if budget else None)
            if matching:
                prod_cards = []
                for p in matching:
                    p_sku = p["sku"]
                    prod_cards.append({
                        "id": p.get("id"),
                        "sku": p_sku,
                        "name": p.get("name"),
                        "price": str(p["price"]) if valid_price(p.get("price")) else None,
                        "mrp": str(p["mrp"]) if valid_price(p.get("mrp")) else None,
                        "category": p.get("category"),
                        "source": p.get("source"),
                        "price_status": p.get("price_status"),
                        "stock_status": p.get("stock_status"),
                        "in_stock": p.get("in_stock"),
                        "stock": p.get("stock"),
                        "image": p.get("images", [""])[0] if p.get("images") else "",
                        "variants_count": len(p.get("variants", [])),
                    })
                action = {
                    "action": "SHOW_PRODUCTS",
                    "products": prod_cards,
                    "count": len(prod_cards),
                }
                updates["interested_skus"] = tuple(p["sku"] for p in matching)
                return current_state.with_updates(**updates), action

    # ── 6. Check for Budget Qualification or Mid-Session Budget Update ───────
    if budget:
        updates["sales_stage"] = SalesStage.QUALIFICATION
        action = {
            "action": "SHOW_PRICE_BANDS",
            "bands": PRICE_BANDS_METADATA,
            "selected_budget": float(budget),
        }
        return current_state.with_updates(**updates), action

    # ── 5. Check for Specific Product Details Inquiry ─────────────────────────
    specific_prod = find_specific_product(user_message, catalog)
    if specific_prod and any(k in msg for k in ["detail", "more", "color", "colour", "variant", "tell me about", "price of", "spec"]):
        prod_sku = specific_prod["sku"]
        updates["sales_stage"] = SalesStage.RECOMMENDATION
        updates["selected_skus"] = (prod_sku,)

        # Prepare verified product view with real variants
        variants = specific_prod.get("variants", [])
        verified_variants = [
            {
                "id": v.get("id"),
                "sku": v.get("sku"),
                "name": v.get("name"),
                "stock": v.get("stock"),
            }
            for v in variants if v.get("id") and v.get("sku")
        ]

        action = {
            "action": "SHOW_PRODUCT",
            "sku": prod_sku,
            "product": {
                "id": specific_prod.get("id"),
                "variant_id": specific_prod.get("variant_id"),
                "sku": prod_sku,
                "name": specific_prod.get("name"),
                "price": str(specific_prod["price"]) if valid_price(specific_prod.get("price")) else None,
                "mrp": str(specific_prod["mrp"]) if valid_price(specific_prod.get("mrp")) else None,
                "category": specific_prod.get("category"),
                "source": specific_prod.get("source"),
                "price_status": specific_prod.get("price_status"),
                "stock_status": specific_prod.get("stock_status"),
                "in_stock": specific_prod.get("in_stock"),
                "stock": specific_prod.get("stock"),
                "description": specific_prod.get("description", ""),
                "images": specific_prod.get("images", []),
                "product_url": specific_prod.get("product_url", ""),
                "variants": verified_variants,
            },
        }
        return current_state.with_updates(**updates), action

    # ── 6. Check for Product Discovery / Recommendations ──────────────────────
    product_keywords = ["bottle", "drinkware", "mug", "cup", "tumbler", "bowl", "bundle", "gift", "kitchen", "dining", "product", "recommend", "show me", "collection", "options"]
    if any(k in msg for k in product_keywords):
        updates["sales_stage"] = SalesStage.PRODUCT_DISCOVERY
        matching = find_matching_products(user_message, catalog, max_items=4, max_budget=float(current_state.budget) if current_state.budget is not None else None)
        if matching:
            prod_cards = []
            for p in matching:
                p_sku = p["sku"]
                prod_cards.append({
                    "id": p.get("id"),
                    "sku": p_sku,
                    "name": p.get("name"),
                    "price": str(p["price"]) if valid_price(p.get("price")) else None,
                    "mrp": str(p["mrp"]) if valid_price(p.get("mrp")) else None,
                    "category": p.get("category"),
                    "source": p.get("source"),
                        "price_status": p.get("price_status"),
                        "stock_status": p.get("stock_status"),
                        "in_stock": p.get("in_stock"),
                    "stock": p.get("stock"),
                    "image": p.get("images", [""])[0] if p.get("images") else "",
                    "variants_count": len(p.get("variants", [])),
                })
            action = {
                "action": "SHOW_PRODUCTS",
                "products": prod_cards,
                "count": len(prod_cards),
            }
            updates["sales_stage"] = SalesStage.RECOMMENDATION
            updates["interested_skus"] = tuple(p["sku"] for p in matching)
            return current_state.with_updates(**updates), action

    # ── 7. Check for Category Inquiries ───────────────────────────────────────
    category_keywords = ["category", "categories", "what do you have", "what do you sell", "range", "types"]
    if any(k in msg for k in category_keywords):
        updates["sales_stage"] = SalesStage.INTENT
        action = {
            "action": "SHOW_CATEGORIES",
            "categories": CATEGORIES_METADATA,
        }
        return current_state.with_updates(**updates), action

    # ── 8. Greeting Phase ─────────────────────────────────────────────────────
    greeting_keywords = ["hi", "hello", "hey", "namaste", "good morning", "good afternoon", "good evening", "start"]
    if current_state.sales_stage == SalesStage.GREETING or any(re.search(rf"\b{k}\b", msg) for k in greeting_keywords):
        updates["sales_stage"] = SalesStage.GREETING
        action = {
            "action": "SHOW_CATEGORIES",
            "categories": CATEGORIES_METADATA,
        }
        return current_state.with_updates(**updates), action

    # ── 9. Default: Stay in current stage or transition to inquiry ────────────
    return current_state.with_updates(**updates), None
