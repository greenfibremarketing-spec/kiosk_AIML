"""Deterministic Sales Transition Engine for Greeny AI Kiosk.

Transitions the conversation through structured sales stages:
GREETING -> ASK_NAME -> ASK_CATEGORY (INTENT) -> SHOW_PRODUCTS (PRODUCT_DISCOVERY)
-> PRODUCT_DETAIL (RECOMMENDATION) -> PRODUCT_QUESTIONS (OBJECTION_HANDLING)
-> B2B_QUALIFICATION (QUALIFICATION) -> QUOTE_REQUEST (CONVERSION)
-> HUMAN_HANDOFF / COMPLETED.

Emits structured UI actions with currently valid Green Fibre product IDs and variant IDs:
- SHOW_CATEGORIES
- ASK_NAME
- SHOW_PRICE_BANDS
- SHOW_PRODUCTS
- SHOW_PRODUCT
- CONFIRM_CONTACT_CONSENT
- COLLECT_PHONE
- REQUEST_HUMAN
"""

from decimal import Decimal
import logging
import uuid
from app.guided_contract import GuidedCommandError
from app.greenfibre_repository import ProductAPIError
import re
from typing import Any, Dict, List, Literal, Optional, Tuple
from pydantic import BaseModel, Field

from app.sales_state import SalesStage, SalesState
from app.tools import _load_catalog, get_product_repository
from app.product_repository import product_view, is_active, valid_price

logger = logging.getLogger("greenie.sales_engine")

# Legacy visual metadata retained for imports; live options use catalog_categories().
CATEGORIES_METADATA = [
    {"id": "drinkware", "name": "Drinkware & Bottles", "icon": "bottle"},
    {"id": "kitchen_dining", "name": "Kitchen & Dining", "icon": "utensils"},
    {"id": "office_desk", "name": "Office & Desk", "icon": "briefcase"},
    {"id": "gift_bundles", "name": "Gift Sets & Bundles", "icon": "gift"},
    {"id": "corporate_b2b", "name": "Corporate & Bulk Gifting", "icon": "building"},
]

SHOW_ALL_OPTION = {"id": "all", "name": "Show Everything", "icon": "grid"}

PRICE_BANDS_METADATA = [
    {"id": "under_500", "label": "Under ₹500", "min": 0, "max": 500},
    {"id": "500_1000", "label": "₹500 - ₹1,000", "min": 500, "max": 1000},
    {"id": "1000_1500", "label": "₹1,000 - ₹1,500", "min": 1000, "max": 1500},
    {"id": "above_1500", "label": "Above ₹1,500", "min": 1500, "max": None},
]


class LLMIntentClassification(BaseModel):
    """Structured Pydantic schema for natural language intent classification."""
    intent: Literal[
        "category",
        "product",
        "name",
        "all",
        "back",
        "restart",
        "skip",
        "help",
        "question",
        "budget",
        "b2b",
        "quote",
        "unavailable",
        "unclear",
    ] = "unclear"
    value: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    entities: Dict[str, Any] = Field(default_factory=dict)


def _build_product_card(p: Dict[str, Any]) -> Dict[str, Any]:
    """Build a verified product card for SHOW_PRODUCTS action."""
    p_sku = p.get("sku", "")
    return {
        "id": p.get("id"),
        "sku": p_sku,
        "name": p.get("name"),
        "price": str(p["price"]) if valid_price(p.get("price")) else None,
        "mrp": str(p["mrp"]) if valid_price(p.get("mrp")) else None,
        "category": p.get("category"),
        "source": p.get("source"),
        "price_status": p.get("price_status"),
        "stock_status": p.get("stock_status"),
        "stock_checked_at": p.get("stock_checked_at"),
        "in_stock": p.get("in_stock"),
        "stock": p.get("stock"),
        "image": p.get("images", [""])[0] if p.get("images") else "",
        "product_url": p.get("product_url", ""),
        "variants_count": len(p.get("variants", [])),
        "variant_ids": [v.get("id") or v.get("sku") for v in p.get("variants", []) if v.get("id") or v.get("sku")],
    }


def extract_customer_name(text: str, is_name_prompt: bool = False) -> Optional[str]:
    """Extract customer first name from introductory phrases or single-word inputs."""
    cleaned = text.strip()
    patterns = [
        r"(?:my name is|i am|i'm|call me|myself)\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)",
        r"(?:naam hai|mera naam)\s+([A-Za-z]+)",
        r"(?:naam|name)\s*[:=]\s*([A-Za-z]+)",
    ]
    for pat in patterns:
        m = re.search(pat, cleaned, re.IGNORECASE)
        if m:
            raw_match = m.group(1).strip()
            parts = re.split(r"\s+(?:and|who|from|here|want|need|looking|interested|for)\b", raw_match, flags=re.IGNORECASE)
            name = parts[0].strip()
            name_words = set(name.lower().split())
            stop_words = {
                "looking", "searching", "for", "here", "greeny", "interested", "shopping",
                "buying", "corporate", "a", "an", "the", "just", "skip", "no", "nahi", "not"
            }
            if name and not (name_words & stop_words):
                return name.title()

    # If the customer is at the ASK_NAME state, accept clean 1 or 2 word names
    if is_name_prompt:
        lower = cleaned.lower()
        skip_words = {
            "skip", "skip name", "next", "chodo", "aage", "no", "nahi", "nahin", "none",
            "don't ask", "dont ask", "no name", "not sharing", "cancel", "help", "human",
            "back", "i don't want to share my name", "dont want to share my name"
        }
        if lower in skip_words or any(lower.startswith(w) for w in ["skip", "no name", "dont", "don't", "nahi"]):
            return None
        words = re.findall(r"[A-Za-z]+", cleaned)
        if 1 <= len(words) <= 2:
            first_word = words[0].title()
            stop_words = {
                "hello", "hi", "hey", "greeny", "what", "who", "show", "tell", "bottle",
                "bottles", "mug", "mugs", "gift", "all", "help", "yes", "ok", "okay"
            }
            if first_word.lower() not in stop_words and len(first_word) >= 2:
                return first_word

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
    # Normalize common ASR misspellings
    for typo, target in [
        ("vyora", "viora"), ("boul", "bowl"), ("tumler", "tumbler"),
        ("tumblar", "tumbler"), ("botle", "bottle"), ("botel", "bottle"), ("botal", "bottle"),
        ("paani ki bottle", "bottle"), ("chai ka cup", "mug"), ("bartan", "kitchen dining")
    ]:
        q_lower = q_lower.replace(typo, target)

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
        tags = [str(t).lower() for t in p.get("tags", [])]

        score = 0
        if q_lower in name:
            score += 3
        if any(term in name for term in q_lower.split() if len(term) > 3):
            score += 2
        if any(term in cat or term in name for term in ["bottle", "drinkware", "cup", "mug", "tumbler", "bowl", "bundle", "kitchen", "dining", "desk", "office"] if term in q_lower):
            score += 2
        if any(term in desc or any(term in t for t in tags) for term in ["birthday", "wedding", "festival", "diwali", "corporate", "gift"] if term in q_lower):
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
    if q_lower.startswith('select_product:'):
        exact = query.split(':', 1)[1].strip()
        for product in products:
            if product.get('sku') == exact:
                return product
            for variant in product.get('variants', []):
                if variant.get('sku') == exact:
                    return {**product, 'sku': exact, 'variant_id': variant.get('id'),
                            'variant_name': variant.get('name'), 'images': variant.get('images', []), 'stock': variant.get('stock'),
                            'in_stock': variant['stock'] > 0 if type(variant.get('stock')) is int else None,
                            'stock_status': 'in_stock' if type(variant.get('stock')) is int and variant['stock'] > 0 else 'out_of_stock' if variant.get('stock') == 0 else 'unknown'}
        return None


    # ASR typo replacements
    for typo, target in [
        ("vyora", "viora"), ("boul", "bowl"), ("tumler", "tumbler"),
        ("tumblar", "tumbler"), ("botle", "bottle"), ("botel", "bottle"),
        ("mugg", "mug"), ("coffe", "coffee"),
    ]:
        q_lower = q_lower.replace(typo, target)

    # 0. Structured product selector action: "select_product:<sku_or_id>"
    if "select_product:" in q_lower:
        target = q_lower.split("select_product:")[1].strip()
        for p in products:
            if p.get("sku", "").lower() == target or p.get("id", "").lower() == target or (p.get("website_sku") or "").lower() == target:
                return p
            for v in p.get("variants", []):
                if v.get("sku", "").lower() == target or v.get("id", "").lower() == target:
                    return {
                        **p, 'sku': v['sku'], 'variant_id': v.get('id'),
                        'variant_name': v.get('name'), 'stock': v.get('stock'),
                        'in_stock': v['stock'] > 0 if type(v.get('stock')) is int else None,
                    }

    for p in products:
        name = p.get("name", "").lower()
        sku = p.get("sku", "").lower()
        pid = p.get("id", "").lower()
        website_sku = (p.get("website_sku") or "").lower()

        for variant in p.get('variants', []):
            if variant.get('sku') and variant['sku'].lower() in q_lower:
                return {
                    **p, 'sku': variant['sku'], 'variant_id': variant.get('id'),
                    'variant_name': variant.get('name'), 'images': variant.get('images', []), 'stock': variant.get('stock'),
                    'in_stock': variant['stock'] > 0 if type(variant.get('stock')) is int else None,
                }
        # 1. Exact canonical SKU or ID match
        if (sku and sku in q_lower) or (pid and pid in q_lower) or (website_sku and website_sku in q_lower):
            return p

        # 2. Substantial name match
        if len(name) > 4 and name in q_lower:
            return p

    # 3. Query specifies distinctive words of product name (e.g., "Statement Mug", "Flora Bowl")
    stop_words = {"tell", "details", "about", "product", "this", "that", "show", "give", "please", "have", "with", "from", "want", "like", "need", "your", "what", "know", "more", "item"}
    query_tokens = [w for w in re.findall(r'[a-z0-9]+', q_lower) if len(w) >= 3 and w not in stop_words]
    if query_tokens:
        for p in products:
            name = p.get("name", "").lower()
            if all(w in name for w in query_tokens):
                return p

    # 4. Model keyword matching with volume disambiguation
    for p in products:
        name = p.get("name", "").lower()
        for word in ["viora", "statement", "classic mug", "eco bottle", "copper", "tumbler", "bowl", "coffee mug", "cutlery"]:
            if word in q_lower and word in name:
                if "900" in q_lower and "900" in name:
                    return p
                if "400" in q_lower and "400" in name:
                    return p
                return p
    return None


def classify_intent_deterministic(
    user_message: str,
    current_state: SalesState,
    catalog: Optional[Dict[str, Any]] = None,
) -> Optional[LLMIntentClassification]:
    """Fast Layer-1 deterministic intent classifier without LLM latency."""
    msg = user_message.strip().lower()

    if any(k in msg for k in ["start over", "restart", "reset", "shuru se"]):
        return LLMIntentClassification(intent="restart", confidence=1.0)

    if msg in ("back", "go back", "piche jao", "previous", "navigate_back"):
        return LLMIntentClassification(intent="back", confidence=1.0)

    if any(k in msg for k in ["speech_done", "greeting_done", "speech done", "greeting done"]):
        return LLMIntentClassification(intent="name", confidence=1.0, entities={"trigger": "speech_done"})

    skip_phrases = ["skip", "skip name", "next", "i don't want to share my name", "dont want to share my name", "naam nahi batana", "no name", "chodo"]
    if current_state.sales_stage == SalesStage.ASK_NAME and any(k in msg for k in skip_phrases):
        return LLMIntentClassification(intent="skip", confidence=1.0)

    if re.search(r"\b(show everything|show all|all products|sab dikhao|pura collection)\b", msg) or msg in ("all", "select_category:all"):
        return LLMIntentClassification(intent="all", confidence=1.0)

    human_phrases = ["human", "store manager", "sales manager", "store associate", "store staff", "talk to human", "speak with human", "madad"]
    if any(p in msg for p in human_phrases):
        return LLMIntentClassification(intent="help", confidence=1.0)

    return None


def _determine_sales_transition(
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
    - Handles human handoff, B2B qualification, budget, categories, products,
      speech-done acknowledgement, Back navigation, Restart, and Skip Name.
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
        except Exception as exc:
            raise ProductAPIError("Product information is unavailable.") from None

    catalog = {**catalog, "products": [
        {**p, **product_view(p)} for p in catalog.get("products", [])
        if p.get("sku") and is_active(p)
    ]}
    updates: Dict[str, Any] = {}
    action: Optional[Dict[str, Any]] = None

    # Track previous stage for Back navigation
    updates["previous_stage"] = current_state.sales_stage

    # ── 1. Check for Restart / Start Over ──────────────────────────────────────
    if any(k in msg for k in ["start over", "restart", "reset", "shuru se"]):
        updates["sales_stage"] = SalesStage.GREETING
        updates["customer_name"] = None
        updates["selected_skus"] = ()
        updates["interested_skus"] = ()
        updates["budget"] = None
        updates["quantity"] = None
        updates["occasion"] = None
        action = {
            "action": "SHOW_CATEGORIES",
            "categories": catalog_categories(catalog),
            "show_all": SHOW_ALL_OPTION,
            "message": "Hi! I'm Greeny from Green Fibre. Welcome!",
        }
        return current_state.with_updates(**updates), action

    # ── 2. Check for Back Navigation ──────────────────────────────────────────
    if msg in ("back", "go back", "piche jao", "previous", "navigate_back"):
        if current_state.selected_skus:
            # Back from product detail -> product list
            updates["selected_skus"] = ()
            updates["sales_stage"] = SalesStage.RECOMMENDATION
            matching = [p for p in catalog.get("products", []) if p.get("sku") in current_state.interested_skus]
            if not matching:
                matching = catalog.get("products", [])[:4]
            prod_cards = [_build_product_card(p) for p in matching]
            action = {
                "action": "SHOW_PRODUCTS",
                "products": prod_cards,
                "count": len(prod_cards),
                "message": "Going back to the product list. Which one would you like to explore?",
            }
            return current_state.with_updates(**updates), action
        elif current_state.sales_stage in (SalesStage.RECOMMENDATION, SalesStage.PRODUCT_DISCOVERY, SalesStage.SHOW_PRODUCTS, SalesStage.B2B_QUALIFICATION, SalesStage.QUOTE_REQUEST, SalesStage.HUMAN_HANDOFF):
            # Back from products -> categories
            updates["sales_stage"] = SalesStage.INTENT
            action = {
                "action": "SHOW_CATEGORIES",
                "categories": catalog_categories(catalog),
                "show_all": SHOW_ALL_OPTION,
                "message": "Taking you back to our categories. What are you looking for today?",
            }
            return current_state.with_updates(**updates), action
        elif current_state.sales_stage in (SalesStage.INTENT, SalesStage.ASK_CATEGORY):
            # Back from categories -> ask name
            updates["sales_stage"] = SalesStage.ASK_NAME
            action = {
                "action": "ASK_NAME",
                "prompt": "What should I call you?",
                "skip_allowed": True,
                "options": [{"id": "skip", "label": "Skip"}],
                "message": "Going back. What should I call you?",
            }
            return current_state.with_updates(**updates), action
        elif current_state.sales_stage == SalesStage.ASK_NAME:
            # Back from ask name -> greeting
            updates["sales_stage"] = SalesStage.GREETING
            action = {
                "action": "SHOW_CATEGORIES",
                "categories": catalog_categories(catalog),
                "show_all": SHOW_ALL_OPTION,
                "message": "Hi! I'm Greeny from Green Fibre. Welcome!",
            }
            return current_state.with_updates(**updates), action

    # ── 3. Speech Completion Acknowledgement (Greeting -> Ask Name) ───────────
    if msg in ("speech_done", "greeting_done") or any(k in msg for k in ["speech done", "greeting done"]):
        updates["sales_stage"] = SalesStage.ASK_NAME
        action = {
            "action": "ASK_NAME",
            "prompt": "What should I call you?",
            "skip_allowed": True,
            "options": [{"id": "skip", "label": "Skip"}],
            "message": "What should I call you?",
        }
        return current_state.with_updates(**updates), action

    # ── 4. Ask Name Stage: Name Given, Skipped, or Refused ────────────────────
    skip_name_phrases = [
        "skip_name", "skip name", "skip", "next", "i don't want to share my name",
        "dont want to share my name", "naam nahi batana", "no name", "naam nahi", "chodo", "aage",
        "don't want to share", "dont want to share", "not sharing", "refuse", "will not share", "won't share",
        "no", "nahi", "nahin"
    ]
    if current_state.sales_stage == SalesStage.ASK_NAME:
        if any(re.search(r"\b" + re.escape(k) + r"\b", msg) for k in skip_name_phrases):
            updates["customer_name"] = None
            updates["sales_stage"] = SalesStage.INTENT
            action = {
                "action": "SHOW_CATEGORIES",
                "categories": catalog_categories(catalog),
                "show_all": SHOW_ALL_OPTION,
                "message": "No problem! What are you looking for today?",
            }
            return current_state.with_updates(**updates), action

        name = extract_customer_name(user_message, is_name_prompt=True)
        if name:
            updates["customer_name"] = name
            updates["sales_stage"] = SalesStage.INTENT
            action = {
                "action": "SHOW_CATEGORIES",
                "categories": catalog_categories(catalog),
                "show_all": SHOW_ALL_OPTION,
                "message": f"Nice to meet you, {name}! What are you looking for today?",
            }
            return current_state.with_updates(**updates), action

    # Check for Name Sharing in any stage
    extracted_name = extract_customer_name(user_message, is_name_prompt=False)
    if extracted_name:
        updates["customer_name"] = extracted_name

    # ── 5. Check for Contact Refusal / Consent Decline / Consent Revocation ───
    refusal_keywords = [
        "don't ask", "dont ask", "no phone", "no number", "decline", "not sharing",
        "not now", "no contact", "do not consent", "don't consent", "nahi", "don't want to share", "dont want to share",
        "skip phone", "refuse", "will not share", "won't share", "not share", "not giving", "cannot share"
    ]
    is_name_refusal = ("name" in msg or "naam" in msg) and not any(c in msg for c in ["phone", "number", "call", "contact"])
    if not is_name_refusal:
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
            "action": "COLLECT_PHONE",
            "consent_scope": "quotation_contact_only",
            "submission_enabled": False,
        }

    # ── 6. Check for Human Handoff Request ────────────────────────────────────
    human_phrases = [
        "human", "store manager", "sales manager", "store associate", "store staff",
        "talk to someone", "speak to someone", "call someone", "connect with someone",
        "connect with me", "reach out to me", "store executive", "sales executive",
        "human agent", "talk to human", "speak with human", "madad"
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
    if extracted_name:
        updates["customer_name"] = extracted_name

    # ── 7. Check for Customization Requests (at any stage) ────────────────────
    if any(k in msg for k in ["custom logo", "logo", "engrav", "branding", "personalized", "print"]):
        customizations = set(current_state.customization_requirements)
        customizations.add("Logo Engraving")
        updates["customization_requirements"] = tuple(customizations)

    # ── 8. Check for B2B Bulk Qualification ───────────────────────────────────
    b2b_keywords = ["corporate", "b2b", "bulk", "wholesale", "company", "employee", "diwali gift", "diwali gifts", "clients", "offices", "bulk order"]
    qty = extract_quantity(user_message)
    is_b2b = any(k in msg for k in b2b_keywords) or (qty is not None and qty >= 20)

    quote_callback_phrases = [
        "send quote", "send a quote", "share quote", "call me", "contact me",
        "take my number", "share on whatsapp", "email quote", "send quotation",
        "give quote", "give me a quote", "quotation callback", "send me quote",
        "call with pricing", "reach out", "quotation", "quote", "whatsapp",
        "prepare quote", "prepare a quote", "prepare quotation", "prepare a quotation",
        "formal quote", "formal quotation", "prepare a formal"
    ]
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

        if budget or any(k in msg for k in ["gift", "bottle", "product", "recommend", "show me", "options", "what do you have"]):
            updates["sales_stage"] = SalesStage.RECOMMENDATION
            matching = find_matching_products(user_message, catalog, max_items=4, max_budget=float(budget) if budget else None)
            if matching:
                prod_cards = [_build_product_card(p) for p in matching]
                action = {
                    "action": "SHOW_PRODUCTS",
                    "products": prod_cards,
                    "count": len(prod_cards),
                }
                updates["interested_skus"] = tuple(p["sku"] for p in matching)
                return current_state.with_updates(**updates), action

    # ── 9. Check for Budget Qualification or Mid-Session Budget Update ───────
    if budget:
        updates["sales_stage"] = SalesStage.QUALIFICATION
        action = {
            "action": "SHOW_PRICE_BANDS",
            "bands": PRICE_BANDS_METADATA,
            "selected_budget": float(budget),
        }
        return current_state.with_updates(**updates), action

    # ── 10. Check for Specific Product Details Inquiry ────────────────────────
    specific_prod = find_specific_product(user_message, catalog)
    if specific_prod and ("select_product:" in msg or any(k in msg for k in ["detail", "more", "color", "colour", "variant", "tell me about", "price of", "spec", "bottle", "mug", "bowl", "tumbler"])):
        # Avoid treating generic queries like "show bottles" as single product detail
        is_generic_list_req = any(w in msg for w in ["show me your bottles", "bottles collection", "show bottles", "list bottles", "all bottles", "show mugs", "mugs collection"])
        if not is_generic_list_req or "select_product:" in msg:
            prod_sku = specific_prod["sku"]
            updates["sales_stage"] = SalesStage.RECOMMENDATION
            updates["selected_skus"] = (prod_sku,)

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
                    "stock_checked_at": specific_prod.get("stock_checked_at"),
                    "in_stock": specific_prod.get("in_stock"),
                    "stock": specific_prod.get("stock"),
                    "description": specific_prod.get("description", ""),
                    "images": specific_prod.get("images", []),
                    "product_url": specific_prod.get("product_url", ""),
                    "variants": verified_variants,
                },
                "message": f"Here is our {specific_prod.get('name')}. Would you like to see its colours, price or other details?",
            }
            return current_state.with_updates(**updates), action

    # ── 11. Check for "Show Everything" / "Show All" ──────────────────────────
    if re.search(r"\b(show everything|show all|all products|sab dikhao|pura collection)\b", msg) or msg in ("all", "select_category:all"):
        updates["sales_stage"] = SalesStage.RECOMMENDATION
        all_products = [p for p in catalog.get("products", []) if p.get("sku") and is_active(p)]
        prod_cards = [_build_product_card(p) for p in all_products]
        action = {
            "action": "SHOW_PRODUCTS",
            "products": prod_cards,
            "count": len(prod_cards),
            "category": "All Products",
            "message": "Here is our full collection. Which product would you like to see?",
        }
        updates["interested_skus"] = tuple(p["sku"] for p in all_products)
        return current_state.with_updates(**updates), action

    # ── 12. Structured Category Selection: "select_category:<id>" ─────────────
    if "select_category:" in msg:
        cat_id = msg.split("select_category:")[1].strip()
        options = {option['id']: option['name'] for option in catalog_categories(catalog)}
        if cat_id not in options:
            raise GuidedCommandError('Unknown category.')
        cat_name = options[cat_id]
        updates['sales_stage'] = SalesStage.RECOMMENDATION
        updates['selected_category'] = cat_name
        matching = [p for p in catalog.get('products', []) if p.get('category') == cat_name]
        prod_cards = [_build_product_card(p) for p in matching]
        action = {
            "action": "SHOW_PRODUCTS",
            "products": prod_cards,
            "count": len(prod_cards),
            "category": cat_name,
            "message": f"Here are our {cat_name} options. Which one would you like to explore?",
        }
        updates["interested_skus"] = tuple(p["sku"] for p in matching)
        return current_state.with_updates(**updates), action

    # ── 13. Shopping Occasion Requests (treated as search filters) ────────────
    occasion_map = {
        "birthday": "Birthday Gifting",
        "wedding": "Wedding Gifting",
        "festival": "Festival Gifting",
        "diwali": "Diwali Gifting",
    }
    for occ_key, occ_label in occasion_map.items():
        if occ_key in msg:
            updates["occasion"] = occ_label
            updates["sales_stage"] = SalesStage.RECOMMENDATION
            matching = find_matching_products(occ_key, catalog, max_items=4)
            prod_cards = [_build_product_card(p) for p in matching]
            action = {
                "action": "SHOW_PRODUCTS",
                "products": prod_cards,
                "count": len(prod_cards),
                "occasion": occ_label,
                "message": f"Here are some great options for {occ_label}. Which one would you like to see?",
            }
            updates["interested_skus"] = tuple(p["sku"] for p in matching)
            return current_state.with_updates(**updates), action

    # ── 14. Product Discovery / Recommendations ───────────────────────────────
    product_keywords = [
        "bottle", "bottles", "paani ki bottle", "drinkware", "mug", "mugs", "cup",
        "tumbler", "bowl", "bundle", "gift", "kitchen", "dining", "product", "recommend",
        "show me", "collection", "options", "chai ka cup", "bartan"
    ]
    if any(k in msg for k in product_keywords):
        updates["sales_stage"] = SalesStage.PRODUCT_DISCOVERY
        matching = find_matching_products(
            user_message,
            catalog,
            max_items=4,
            max_budget=float(current_state.budget) if current_state.budget is not None else None,
        )
        if matching:
            prod_cards = [_build_product_card(p) for p in matching]
            action = {
                "action": "SHOW_PRODUCTS",
                "products": prod_cards,
                "count": len(prod_cards),
            }
            updates["sales_stage"] = SalesStage.RECOMMENDATION
            updates["interested_skus"] = tuple(p["sku"] for p in matching)
            return current_state.with_updates(**updates), action

    # ── 15. Check for Category Inquiries ──────────────────────────────────────
    category_keywords = ["category", "categories", "what do you have", "what do you sell", "range", "types"]
    if any(k in msg for k in category_keywords):
        updates["sales_stage"] = SalesStage.INTENT
        action = {
            "action": "SHOW_CATEGORIES",
            "categories": catalog_categories(catalog),
            "show_all": SHOW_ALL_OPTION,
        }
        return current_state.with_updates(**updates), action

    # ── 16. Greeting Phase ────────────────────────────────────────────────────
    greeting_keywords = ["hi", "hello", "hey", "namaste", "good morning", "good afternoon", "good evening", "start"]
    if current_state.sales_stage == SalesStage.GREETING or any(re.search(rf"\b{k}\b", msg) for k in greeting_keywords):
        updates["sales_stage"] = SalesStage.GREETING
        action = {
            "action": "SHOW_CATEGORIES",
            "categories": catalog_categories(catalog),
            "show_all": SHOW_ALL_OPTION,
        }
        return current_state.with_updates(**updates), action

    # ── 17. Default: Stay in current stage ────────────────────────────────────
    return current_state.with_updates(**updates), None


def catalog_categories(catalog):
    names = sorted({p.get('category') for p in catalog.get('products', []) if p.get('category') and is_active(p)})
    return [{'id': re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-'), 'name': name} for name in names]


def _greeting(state):
    speech_id = uuid.uuid4().hex
    state = state.with_updates(sales_stage=SalesStage.GREETING, guided_mode=True, greeting_speech_id=speech_id)
    return state, {'action': 'SHOW_CATEGORIES', 'categories': [], 'input_enabled': False,
                   'awaiting_speech_done': True, 'speech_id': speech_id,
                   'message': "Hi! I'm Greeny from Green Fibre. Welcome!"}


def determine_sales_transition(current_state, user_message, tool_outputs=None, catalog=None, command=None):
    """Guided command boundary; legacy conversational engine stays in place."""
    msg = user_message.strip().lower()
    action = command.get('action') if command else None
    event_id = command.get('turn_id') if command else None
    if command and (not event_id or event_id in current_state.processed_event_ids):
        raise GuidedCommandError('Missing or duplicate turn_id.')
    if action == 'SPEECH_DONE':
        if current_state.sales_stage != SalesStage.GREETING or not current_state.greeting_speech_id or command.get('speech_id') != current_state.greeting_speech_id:
            raise GuidedCommandError('Speech acknowledgement does not match the pending greeting.')
        next_state = current_state.with_updates(sales_stage=SalesStage.ASK_NAME, greeting_speech_id=None)
        screen = {'action': 'ASK_NAME', 'prompt': 'What should I call you?', 'skip_allowed': True,
                  'message': 'What should I call you?'}
    elif action == 'RESTART' or msg in ('restart', 'start over', 'shuru se'):
        clean = SalesState(session_id=current_state.session_id, kiosk_id=current_state.kiosk_id, channel=current_state.channel,
                           processed_event_ids=current_state.processed_event_ids)
        next_state, screen = _greeting(clean)
    elif current_state.greeting_speech_id:
        raise GuidedCommandError('A valid speech-completion acknowledgement is required.')
    elif msg in ('speech_done', 'greeting_done'):
        raise GuidedCommandError('Speech completion requires a structured acknowledgement.')
    elif current_state.sales_stage == SalesStage.GREETING and re.fullmatch(r'(hi|hello|hey|namaste|start)\b.*', msg):
        next_state, screen = _greeting(current_state)
    else:
        if action in ('SET_NAME', 'SKIP_NAME') and current_state.sales_stage != SalesStage.ASK_NAME:
            raise GuidedCommandError('Name entry is not pending.')
        if action in ('SELECT_CATEGORY', 'SHOW_ALL', 'SELECT_PRODUCT', 'NAVIGATE_BACK') and current_state.sales_stage == SalesStage.GREETING:
            raise GuidedCommandError('Complete the greeting first.')
        if catalog is None:
            try:
                repository = get_product_repository()
                snapshot = repository.catalog()
                products = []
                for candidate in snapshot.get('products', []):
                    if not candidate.get('sku'):
                        continue
                    fresh = repository.get(candidate['sku'])
                    if fresh and is_active(fresh):
                        view = product_view(fresh)
                        if fresh.get('source') == 'greenfibre_api' and view['stock_status'] != 'in_stock':
                            continue
                        products.append({**fresh, **view})
                catalog = {'products': products}
            except Exception:
                raise ProductAPIError('Current product information is unavailable.') from None
        # Exact displayed category names have the same meaning for text, ASR and taps.
        normalized = user_message
        for category in catalog_categories(catalog):
            name = category['name'].lower()
            if msg in (name, 'show ' + name, 'show me ' + name, 'show ' + name + 's'):
                normalized = 'select_category:' + category['id']
                break
        if action == 'SELECT_CATEGORY' and command['category_id'] not in {c['id'] for c in catalog_categories(catalog)}:
            raise GuidedCommandError('Unknown category.')
        exact_products = [p for p in catalog.get('products', []) if msg in (
            str(p.get('sku', '')).lower(), str(p.get('name', '')).lower(),
            'tell me about ' + str(p.get('name', '')).lower(),
        )]
        selected = find_specific_product('select_product:' + command['sku'], catalog) if action == 'SELECT_PRODUCT' else exact_products[0] if len(exact_products) == 1 else None
        if selected:
            view = product_view(selected)
            next_state = current_state.with_updates(selected_skus=(view['sku'],), sales_stage=SalesStage.RECOMMENDATION)
            screen = {'action': 'SHOW_PRODUCT', 'sku': view['sku'], 'product': view,
                      'message': f"Here is our {view['name']}. Would you like to see its colours, price or other details?"}
        elif action == 'SET_NAME':
            next_state = current_state.with_updates(customer_name=command['name'].title(), sales_stage=SalesStage.INTENT)
            screen = {'action': 'SHOW_CATEGORIES', 'categories': catalog_categories(catalog), 'show_all': SHOW_ALL_OPTION,
                      'message': f"Nice to meet you, {command['name']}! What are you looking for today?"}
        else:
            next_state, screen = _determine_sales_transition(current_state, normalized, tool_outputs, catalog)
        if action == 'SELECT_PRODUCT' and (not screen or screen.get('action') != 'SHOW_PRODUCT'):
            raise GuidedCommandError('Selected product is unavailable.')
        if next_state.sales_stage == SalesStage.GREETING:
            next_state, screen = _greeting(next_state)
        elif current_state.guided_mode:
            screen_type = (screen or {}).get('action')
            stages = {'ASK_NAME': SalesStage.ASK_NAME, 'SHOW_CATEGORIES': SalesStage.ASK_CATEGORY,
                      'SHOW_PRODUCTS': SalesStage.SHOW_PRODUCTS, 'SHOW_PRODUCT': SalesStage.PRODUCT_DETAIL,
                      'REQUEST_HUMAN': SalesStage.HUMAN_HANDOFF,
                      'CONFIRM_CONTACT_CONSENT': SalesStage.QUOTE_REQUEST, 'COLLECT_PHONE': SalesStage.QUOTE_REQUEST}
            if next_state.customer_intent == 'b2b' and (extract_quantity(user_message) or extract_budget(user_message) or re.search(r'\b(corporate|bulk|employees?|b2b)\b', msg)) and screen_type not in ('REQUEST_HUMAN', 'CONFIRM_CONTACT_CONSENT', 'COLLECT_PHONE'):
                next_state = next_state.with_updates(sales_stage=SalesStage.B2B_QUALIFICATION)
            elif screen_type in stages:
                next_state = next_state.with_updates(sales_stage=stages[screen_type])
            elif current_state.selected_skus and re.search(r'\b(price|cost|material|care|stock|safe|certif|how|what|does|is)\b', msg):
                next_state = next_state.with_updates(sales_stage=SalesStage.PRODUCT_QUESTIONS)
    if command:
        next_state = next_state.with_updates(processed_event_ids=(*current_state.processed_event_ids[-127:], event_id))
    return next_state, screen
