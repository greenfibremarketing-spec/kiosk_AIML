"""Free conversation agent for Green Fibre kiosk.

Provides intelligent free-form conversational understanding, product catalog query matching,
deterministic budget filtering, and structured UI actions (SHOW_PRODUCTS, SHOW_PRODUCT, SHOW_CATEGORIES, etc.)
while preserving the guided flow state and transitions.
"""

from __future__ import annotations
import difflib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.sales_engine import get_verified_catalog, _build_product_card, catalog_categories
from app.recommendation import build_recommendation_action, RECOMMENDATION_DISCOUNT_PERCENT

logger = logging.getLogger("greenie.conversation_agent")

# In-memory TTL cache for identical questions (30 seconds)
_CONV_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
CACHE_TTL_SECONDS = 30.0

VALID_ACTIONS = {
    "NONE",
    "SHOW_CATEGORIES",
    "SHOW_PRODUCTS",
    "SHOW_PRODUCT",
    "SHOW_ALL",
    "NAVIGATE_BACK",
    "CONFIRM_BUY",
    "SHOW_RECOMMENDATION",
}

# Pre-warmed friendly acknowledgements for instant matches
_ACK_OPENERS = [
    "Sure!",
    "Of course!",
    "Lovely choice!",
    "Let me show you.",
    "Here you go!",
    "Great pick!",
    "One moment.",
]
_ack_index = 0


def get_next_ack_opener() -> str:
    """Return next friendly acknowledgement opener in round-robin order."""
    global _ack_index
    opener = _ACK_OPENERS[_ack_index % len(_ACK_OPENERS)]
    _ack_index += 1
    return opener


def build_compact_catalog(catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build a compact, clean representation of active catalog products for LLM context."""
    compact = []
    for p in catalog.get("products", []):
        sku = p.get("sku")
        if not sku:
            continue
        name = p.get("name", "")
        category = p.get("category", "")
        price = p.get("price") or p.get("salePrice") or 0
        mrp = p.get("mrp") or price
        variants = p.get("variants") or []
        colours = [
            (v.get("colour") or v.get("color") or v.get("name") or "").strip()
            for v in variants
            if v.get("colour") or v.get("color") or v.get("name")
        ]

        desc = p.get("shortDescription") or p.get("description") or ""
        clean_desc = re.sub(r"<[^>]+>", " ", str(desc))
        clean_desc = re.sub(r"\s+", " ", clean_desc).strip()
        first_sent = clean_desc.split(".")[0].strip() if clean_desc else ""
        if len(first_sent) > 100:
            first_sent = first_sent[:97] + "..."

        tags = []
        name_cat = (name + " " + category + " " + clean_desc).lower()
        if any(w in name_cat for w in ["gift", "hamper", "pack", "bundle", "set", "mini"]):
            tags.append("gifting")
        if any(w in name_cat for w in ["bottle", "tumbler", "400"]):
            tags.extend(["kids", "school", "travel"])
        if any(w in name_cat for w in ["mug", "bottle", "coaster", "tissue", "desk"]):
            tags.extend(["office", "work"])
        if any(w in name_cat for w in ["bowl", "dining", "casserole", "canister", "soup", "salad", "storage"]):
            tags.extend(["kitchen", "dining"])
        if "planter" in name_cat:
            tags.extend(["plants", "balcony", "garden"])

        base_price = float(p.get("price") or p.get("salePrice") or p.get("mrp") or 0)
        # Apply 10% kiosk discount on base price so it matches the screen exactly (1199 -> 1079, 1499 -> 1349)
        discounted_price = round(base_price * 0.9) if base_price else 0

        # Category-accurate features
        cat_lower = (category or "").lower()
        if "bottle" in cat_lower or "tumbler" in cat_lower:
            prod_features = "100% upcycled rice husk biocomposite, food-grade stainless steel inner, insulated for hot or cold, hand-wash recommended, BPA-free"
        elif "tableware" in cat_lower or "kitchen" in cat_lower or "dining" in cat_lower or "bowl" in cat_lower or "plate" in cat_lower:
            prod_features = "100% upcycled rice husk biocomposite, zero virgin plastic, food-safe, BPA-free, microwave-reheat safe, dishwasher safe"
        else:
            prod_features = "100% upcycled rice husk biocomposite, durable, lightweight, eco-friendly"

        compact.append({
            "sku": sku,
            "name": name,
            "category": category,
            "price": discounted_price,
            "mrp": int(base_price),
            "colours": list(dict.fromkeys(colours)),
            "stock": p.get("stock", "in_stock"),
            "description": first_sent or "Upcycled rice-husk biocomposite lifestyle product.",
            "tags": list(set(tags)),
            "features": prod_features
        })
    return compact


def parse_budget_intent(text: str) -> Optional[Dict[str, Any]]:
    """Deterministically parse budget constraints from customer message."""
    t = text.lower()

    def _val(num_str: str, mult: Optional[str]) -> float:
        v = float(num_str)
        if mult and mult.lower() in ("k", "thousand"):
            v *= 1000
        return v

    # Range: between X and Y
    m = re.search(r"\b(?:between)\s*(?:rs\.?|inr|₹)?\s*(\d+(?:\.\d+)?)\s*(k)?\s*(?:and|to|-)\s*(?:rs\.?|inr|₹)?\s*(\d+(?:\.\d+)?)\s*(k)?\b", t)
    if m:
        low = _val(m.group(1), m.group(2))
        high = _val(m.group(3), m.group(4))
        mn = min(low, high)
        mx = max(low, high)
        return {
            "min_price": mn,
            "max_price": mx,
            "label": f"Between {int(mn)} and {int(mx)} rupees"
        }

    # Under / Below / Less than / Up to / Within / Max
    m = re.search(r"\b(?:under|below|less than|within|upto|up to|max(?:imum)?)\s*(?:of|is|rs\.?|inr|₹)?\s*(\d+(?:\.\d+)?)\s*(k)?\b", t)
    if m:
        val = _val(m.group(1), m.group(2))
        return {
            "min_price": None,
            "max_price": val,
            "label": f"Under {int(val)} rupees"
        }

    # Budget is X / Budget of X
    m = re.search(r"\b(?:budget\s*(?:is|of)?)\s*(?:rs\.?|inr|₹)?\s*(\d+(?:\.\d+)?)\s*(k)?\b", t)
    if m:
        val = _val(m.group(1), m.group(2))
        return {
            "min_price": None,
            "max_price": val,
            "label": f"Under {int(val)} rupees"
        }

    # Around X / Approx X
    m = re.search(r"\b(?:around|about|approx(?:imately)?)\s*(?:rs\.?|inr|₹)?\s*(\d+(?:\.\d+)?)\s*(k)?\b", t)
    if m:
        val = _val(m.group(1), m.group(2))
        return {
            "min_price": max(0, val * 0.75),
            "max_price": val * 1.25,
            "label": f"Around {int(val)} rupees"
        }

    return None


def filter_catalog_by_budget(
    catalog_products: List[Dict[str, Any]],
    budget_filter: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], bool]:
    """Filter catalog products by parsed budget.

    Returns (matching_products, had_exact_match). If nothing matches under max_price,
    returns the closest options (cheapest above budget) with had_exact_match = False.
    """
    max_p = budget_filter.get("max_price")
    min_p = budget_filter.get("min_price")

    matching = []
    for p in catalog_products:
        p_val = float(p.get("price") or 0)
        if max_p is not None and p_val > max_p:
            continue
        if min_p is not None and p_val < min_p:
            continue
        matching.append(p)

    if matching:
        # Sort descending by price for curated display
        matching.sort(key=lambda x: float(x.get("price") or 0), reverse=True)
        return matching[:6], True

    # None under budget -> return closest (cheapest above budget)
    above = [p for p in catalog_products if float(p.get("price") or 0) > (max_p or 0)]
    above.sort(key=lambda x: float(x.get("price") or 0))
    return above[:6], False


def normalize_query_text(text: str) -> str:
    """Normalize text before matching: lowercase, unit standardization, spoken numbers, filler words."""
    if not text:
        return ""
    t = text.lower().strip()

    # Normalize units: millilitres, milliliters, millilitre, milliliter -> ml; litres/liters -> l
    t = re.sub(r"\bmillilit(?:er|re)s?\b", "ml", t)
    t = re.sub(r"\blit(?:er|re)s?\b", "l", t)
    t = re.sub(r"\bgrams?\b", "g", t)

    # Convert spoken numbers to digits
    number_replacements = [
        (r"\bnine\s+hundred\b", "900"),
        (r"\bfour\s+hundred\b", "400"),
        (r"\bfive\s+hundred\b", "500"),
        (r"\bthree\s+hundred\b", "300"),
        (r"\beight\s+hundred\b", "800"),
        (r"\bseven\s+hundred\b", "700"),
        (r"\bsix\s+hundred\b", "600"),
        (r"\btwo\s+thousand\b", "2000"),
        (r"\bone\s+thousand\b", "1000"),
        (r"\bfifteen\s+hundred\b", "1500"),
        (r"\btwelve\s+hundred\b", "1200"),
        (r"\bfirst\b", "1st"),
        (r"\bsecond\b", "2nd"),
        (r"\bthird\b", "3rd"),
        (r"\bfourth\b", "4th"),
        (r"\bpehla\b", "1st"),
        (r"\bdusra\b", "2nd"),
        (r"\bteesra\b", "3rd"),
        (r"\baakhri\b", "last"),
    ]
    for pat, rep in number_replacements:
        t = re.sub(pat, rep, t)

    # Replace punctuation characters with space (keep hyphens and colons)
    t = re.sub(r"[^\w\s-]", " ", t)

    # Strip filler words and action verbs
    fillers = [
        r"\b(?:can you|could you|please|kindly)\b",
        r"\b(?:i want to see|i want to open|i want to buy|i want to view|i want to look at|i want to check out)\b",
        r"\b(?:take me to the|take me to|give me the|give me|show me the|show me|tell me about the|tell me about)\b",
        r"\b(?:open up the|open the|open up|open)\b",
        r"\b(?:what is the price of the|what is the price of|what's the price of|price of the|price of)\b",
        r"\b(?:details for the|details for|details of the|details of|details)\b",
        r"\b(?:i want|i need|let me see|let me look at|view|see)\b",
    ]
    for f in fillers:
        t = re.sub(f, " ", t)

    t = re.sub(r"\s+", " ", t).strip()
    return t


def score_product_match(query_norm: str, p: Dict[str, Any], raw_query: str = "") -> float:
    """Best-match rule: exact name -> all words matching -> fuzzy with strict spec conflict filters."""
    name = (p.get("name") or "").lower()
    sku = (p.get("sku") or "").lower()
    name_norm = normalize_query_text(name)
    raw_lower = raw_query.lower()

    if sku and (sku in query_norm or sku in raw_lower):
        return 2.0

    # 1. Exact Name Matching Rule
    if query_norm == name_norm:
        return 2.0
    if name_norm and name_norm in query_norm:
        return 1.8

    # 2. Strict Spec Conflict Filters (e.g. 400 ml vs 900 ml, Set of 2 vs Set of 4)
    # Capacity filter (400 vs 900)
    has_900_q = bool(re.search(r"\b900\b", query_norm) or re.search(r"\b900\s*ml\b", raw_lower))
    has_900_p = "900" in name_norm or "900" in sku
    has_400_q = bool(re.search(r"\b400\b", query_norm) or re.search(r"\b400\s*ml\b", raw_lower))
    has_400_p = "400" in name_norm or "400" in sku
    if has_900_q and not has_900_p:
        return 0.0
    if has_400_q and not has_400_p:
        return 0.0

    # Set / Pair quantity filter (2 vs 4)
    has_2_q = bool(re.search(r"\b(2|two|pair of 2|set of 2)\b", raw_lower) or re.search(r"\b(2|set of 2|pair of 2)\b", query_norm))
    has_4_q = bool(re.search(r"\b(4|four|set of 4)\b", raw_lower) or re.search(r"\b(4|set of 4)\b", query_norm))
    has_2_p = "set of 2" in name_norm or "pair of 2" in name_norm or bool(re.search(r"\b2\b", name_norm))
    has_4_p = "set of 4" in name_norm or bool(re.search(r"\b4\b", name_norm))

    if has_2_q and has_4_p and not has_2_p:
        return 0.0
    if has_4_q and has_2_p and not has_4_p:
        return 0.0

    score = 0.0
    if has_900_q and has_900_p:
        score += 0.80
    if has_400_q and has_400_p:
        score += 0.80
    if has_2_q and has_2_p:
        score += 0.60
    if has_4_q and has_4_p:
        score += 0.60

    # Distinctive product keywords
    distinctive_keywords = [
        ("casserole", ["casserole"]),
        ("planter", ["planter"]),
        ("tissue", ["tissue"]),
        ("canister", ["canister"]),
        ("viora", ["viora"]),
        ("flora", ["flora"]),
        ("soup bowl", ["soup bowl"]),
        ("storage bowl", ["storage bowl"]),
        ("family pack", ["family pack"]),
        ("mini gift", ["mini gift", "gift packaging"]),
        ("dining set", ["dining set"]),
        ("classic mug", ["classic mug"]),
        ("statement mug", ["statement mug"]),
    ]
    for term, syns in distinctive_keywords:
        if any(s in query_norm or s in raw_lower for s in syns):
            if any(s in name_norm for s in syns):
                score += 0.50

    # All substantive words matching rule
    q_tokens = set(query_norm.split())
    name_tokens = set(name_norm.split())
    generic = {"the", "a", "an", "for", "in", "of", "and", "our", "item", "product"}
    q_clean = q_tokens - generic
    n_clean = name_tokens - generic
    if n_clean and n_clean.issubset(q_clean):
        score += 0.60
    elif q_clean and n_clean:
        overlap = len(q_clean.intersection(n_clean))
        score += (overlap / max(len(n_clean), 1)) * 0.40

    # String similarity using SequenceMatcher
    sm = difflib.SequenceMatcher(None, query_norm, name_norm).ratio()
    score += sm * 0.35

    return score


def resolve_screen_reference(
    msg_raw: str,
    norm_text: str,
    last_list_skus: Optional[List[str]],
    current_screen: Optional[Dict[str, Any]],
    all_products: List[Dict[str, Any]],
) -> Optional[str]:
    """Resolve ordinal or contextual references against current screen state."""
    if not last_list_skus:
        return None
    raw_l = msg_raw.lower()

    # Ordinal matching: 2nd / second / dusra
    if re.search(r"\b(2nd|second|dusra|item\s*2|product\s*2|number\s*2|no\.?\s*2)\b", raw_l) or "2nd" in norm_text:
        if len(last_list_skus) >= 2:
            return last_list_skus[1]
    # 1st / first / pehla
    if re.search(r"\b(1st|first|pehla|item\s*1|product\s*1|number\s*1|no\.?\s*1)\b", raw_l) or "1st" in norm_text:
        if len(last_list_skus) >= 1:
            return last_list_skus[0]
    # 3rd / third / teesra
    if re.search(r"\b(3rd|third|teesra|item\s*3|product\s*3|number\s*3|no\.?\s*3)\b", raw_l) or "3rd" in norm_text:
        if len(last_list_skus) >= 3:
            return last_list_skus[2]
    # 4th / fourth / chautha
    if re.search(r"\b(4th|fourth|item\s*4|product\s*4|number\s*4|no\.?\s*4)\b", raw_l) or "4th" in norm_text:
        if len(last_list_skus) >= 4:
            return last_list_skus[3]
    # Last one / aakhri
    if re.search(r"\b(last\s*one|the\s*last|aakhri|final\s*one)\b", raw_l) or "last" in norm_text:
        if len(last_list_skus) >= 1:
            return last_list_skus[-1]

    # Comparative matching: cheaper / cheapest
    if re.search(r"\b(cheaper|cheapest|less\s*expensive|lower\s*price)\b", raw_l):
        prods_in_list = [p for p in all_products if p.get("sku") in last_list_skus]
        if prods_in_list:
            cheapest = min(prods_in_list, key=lambda x: float(x.get("price") or 0))
            return cheapest.get("sku")

    # Bigger / larger / bigger one
    if re.search(r"\b(bigger|larger|highest\s*capacity|more\s*capacity)\b", raw_l):
        prods_in_list = [p for p in all_products if p.get("sku") in last_list_skus]
        for p in prods_in_list:
            if "900" in (p.get("name") or "") or "900" in (p.get("sku") or ""):
                return p.get("sku")
        if prods_in_list:
            most_exp = max(prods_in_list, key=lambda x: float(x.get("price") or 0))
            return most_exp.get("sku")

    # This one / that one
    if re.search(r"\b(this\s*one|that\s*one|is\s*ko|yeh\s*wala)\b", raw_l):
        if current_screen and current_screen.get("type") == "product" and current_screen.get("sku"):
            return current_screen.get("sku")
        if last_list_skus:
            return last_list_skus[0]

    return None


def find_product_by_text(text: str, catalog_products: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Match a product mentioned in text by fuzzy scoring."""
    norm = normalize_query_text(text)
    best_p = None
    best_score = 0.0
    for p in catalog_products:
        s = score_product_match(norm, p, text)
        if s > best_score:
            best_score = s
            best_p = p
    if best_score >= 0.60:
        return best_p
    return None


def build_system_prompt(
    compact_catalog: List[Dict[str, Any]],
    current_stage: str = "BROWSING",
    customer_name: Optional[str] = None,
    current_context: Optional[Dict[str, Any]] = None,
    budget_intent: Optional[Dict[str, Any]] = None,
    budget_products: Optional[List[Dict[str, Any]]] = None,
    had_budget_match: bool = False,
    expected_screen_action: Optional[Dict[str, Any]] = None,
    name_status: str = "unknown",
    name_asked_count: int = 0,
    current_screen: Optional[Dict[str, Any]] = None,
) -> str:
    """Build system instructions with catalog data and strict JSON output rules."""
    catalog_lines = []
    for p in compact_catalog:
        tags_str = ", ".join(p.get("tags", []))
        desc_str = p.get("description", "")
        catalog_lines.append(
            f"- SKU: {p['sku']} | {p['name']} | {p['category']} | {p['price']} rupees | tags: {tags_str} | {desc_str}"
        )
    catalog_text = "\n".join(catalog_lines)

    budget_notes = ""
    if budget_intent:
        if had_budget_match:
            matched_names = [f"{p['name']} (₹{p['price']})" for p in (budget_products or [])]
            budget_notes = (
                f"\nBUDGET INTENT DETECTED: {budget_intent.get('label')}\n"
                f"Matching catalog products: {', '.join(matched_names)}.\n"
                f"ACTION MUST BE SHOW_PRODUCTS with matching skus. Highlight the best pick.\n"
            )
        else:
            closest_names = [f"{p['name']} (₹{p['price']})" for p in (budget_products or [])]
            budget_notes = (
                f"\nBUDGET INTENT DETECTED: {budget_intent.get('label')}\n"
                f"NO products are available under this budget. Closest options: {', '.join(closest_names)}.\n"
                f"Acknowledge honestly that nothing is under {budget_intent.get('max_price')}, then show the closest available piece.\n"
            )

    if name_status == "given" and customer_name:
        name_instruction = f"- The customer's name is {customer_name}. Use it naturally at most once every few turns."
    elif name_status == "skipped":
        name_instruction = "- The customer skipped the name. Never ask for it."
    elif name_status == "unknown":
        if name_asked_count == 0 and (current_screen or {}).get("type") in ("home", "greeting"):
            name_instruction = "- Customer name is unknown. You may ask for their name once if appropriate."
        else:
            name_instruction = "- Customer name is unknown. Do not ask for their name."
    else:
        name_instruction = "- Do not ask for customer's name."

    can_ask_name = (
        name_status == "unknown"
        and name_asked_count == 0
        and (current_screen or {}).get("type") in ("home", "greeting")
    )
    if can_ask_name:
        resume_rule = '- Customer name is unknown. You may include a gentle request for their name in resume_prompt (e.g. "Before we start, may I know your name?").'
    else:
        resume_rule = "- Never ask for the customer's name in reply or resume_prompt. Keep resume_prompt empty or gently invite them to explore products."

    screen_prompt_note = ""
    if expected_screen_action:
        act = expected_screen_action.get("action")
        if act == "SHOW_PRODUCT":
            p_sku = expected_screen_action.get("sku") or (expected_screen_action.get("product", {}).get("sku"))
            p_obj = next((p for p in compact_catalog if p["sku"] == p_sku), None)
            if p_obj:
                screen_prompt_note = (
                    f"\nSCREEN DISPLAY: SHOW_PRODUCT {p_obj['name']} ({p_obj['price']} rupees).\n"
                    f"Your spoken reply MUST describe this exact product shown on screen."
                )
        elif act == "SHOW_PRODUCTS":
            prods = expected_screen_action.get("products", [])
            p_names = [p.get("name") for p in prods[:3] if isinstance(p, dict) and p.get("name")]
            screen_prompt_note = (
                f"\nSCREEN DISPLAY: SHOW_PRODUCTS showing {', '.join(p_names)}.\n"
                f"Your spoken reply MUST describe these items shown on screen."
            )
        elif act == "SHOW_CATEGORIES":
            screen_prompt_note = "\nSCREEN DISPLAY: SHOW_CATEGORIES. Your spoken reply should invite them to browse categories on screen."

    return f"""You are Greenie, a confident, warm, positive shop host for Green Fibre (eco-friendly essentials made from upcycled rice husk biocomposite, zero virgin plastic). You speak simple Indian-English and are in charge of the conversation.

CATALOG:
{catalog_text}
{budget_notes}{screen_prompt_note}
CURRENT STATE:
- Stage: {current_stage}
- Customer Name: {customer_name or "Unknown"}
- Current Context: {json.dumps(current_context or {})}
{name_instruction}

BEHAVIOUR & PERSONA RULES:
1. Speak 1 to 2 short sentences (max about 3, under 30 words total). Natural spoken style ("Sure!", "Great question!", "Of course!").
2. Lead the conversation: after answering, gently offer ONE next step ("Would you like to see it in another colour?", "Shall I show you a matching pair-up?", "Would you like me to add this to your cart?").
3. Ask only ONE question at a time. Never repeat the same sentence twice in a row. Vary your wording.
4. Use the customer's name at most once every few turns (never in every reply).
5. GROUNDING (CRITICAL): Only state facts that exist in the product/catalogue data or current screen.
   - NEVER invent product details, stock, delivery times, warranty, policies, offers, discounts, certifications or prices.
   - For bottles (Viora bottles): hand-wash is recommended for the stainless steel inner; bottles are hand-wash only, NOT dishwasher safe.
   - Tableware pieces and bowls are microwave-reheat safe and dishwasher safe.
   - All products have a 10% discount off retail MRP. Viora 400 ml is 1079 rupees; Viora 900 ml is 1349 rupees. Bundle pair-up has an extra {RECOMMENDATION_DISCOUNT_PERCENT}% discount.
   - If asked about delivery or delivery tomorrow, say: "Delivery details and pin-code verification are handled right at checkout." Never promise delivery tomorrow.
   - If the answer to a question is NOT in the data, honestly say: "Sorry, I don't have that detail right now. I can tell you about the material, colours, size and price instead."
   - If user asks for an unlisted item (e.g. laptop, shoes): "I couldn't find that here, but you can browse our categories on screen, or tell me what you need and I'll suggest something."
   - If user asks about unrelated topics (weather, politics, news): "I'm best at helping with our eco-friendly products. What are you looking for today?"
6. Needs mapping:
   - gift, wedding, Diwali, birthday -> gifting products (e.g. Eco Mini Gift Packaging, Mug Sets, Eco Dining Set) -> SHOW_PRODUCTS or SHOW_CATEGORIES
   - kids or school -> bottles (e.g. Viora 400 ml is lightweight for school) -> SHOW_PRODUCTS
   - office -> bottles and mugs -> SHOW_PRODUCTS
   - kitchen or dining -> bowls, casserole, canisters -> SHOW_PRODUCTS or SHOW_PRODUCT
   - plants or balcony -> Table top planter -> SHOW_PRODUCTS or SHOW_PRODUCT
   - compare -> show 2-3 products with SHOW_PRODUCTS and explain difference in one concise sentence
   - recommendations / what goes with item -> action SHOW_RECOMMENDATION
7. No lists read aloud (the screen shows the items). No markdown, bullet points, asterisks, or dashes.
8. Prices must be spoken as plain numbers followed by "rupees" (e.g. "799 rupees"), matching the verified catalog price.

RESUME PROMPT RULE:
{resume_rule}

OUTPUT FORMAT:
Return STRICT JSON ONLY. No preamble, no backticks, no markdown.
{{
  "action": "NONE | SHOW_CATEGORIES | SHOW_PRODUCTS | SHOW_PRODUCT | SHOW_ALL | NAVIGATE_BACK | CONFIRM_BUY | SHOW_RECOMMENDATION",
  "sku": null,
  "skus": [],
  "reply": "1 to 2 short sentences, spoken aloud",
  "category_id": null,
  "filters": {{ "max_price": null, "min_price": null, "tags": [], "colour": null }},
  "resume_prompt": ""
}}"""


def sanitize_spoken_text(text: str) -> str:
    """Normalize unicode spaces, symbols, and formatting for clean TTS and console output."""
    if not text:
        return ""
    t = text
    # Replace non-breaking spaces and zero-width spaces
    t = re.sub(r"[\u202f\u00a0\u200b\u200e\u200f]", " ", t)
    # Replace non-breaking hyphens and unicode dashes with standard ascii hyphen
    t = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2015]", "-", t)
    # Replace unicode quotes
    t = re.sub(r"[\u2018\u2019]", "'", t)
    t = re.sub(r"[\u201c\u201d]", '"', t)
    # Replace currency symbols
    t = re.sub(r"₹\s*(\d+)", r"\1 rupees", t)
    t = re.sub(r"(?:rs\.?|inr)\s*(\d+)", r"\1 rupees", t, flags=re.IGNORECASE)
    # Remove markdown asterisks, hashes, backticks
    t = re.sub(r"[*#`_~]", "", t)
    # Normalize multiple spaces
    t = re.sub(r"\s+", " ", t).strip()
    return t


def clean_json_text(text: str) -> str:
    """Extract raw JSON from LLM response text."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    t = t.strip()
    # Try finding first { and last }
    s = t.find("{")
    e = t.rfind("}")
    if s != -1 and e != -1 and e > s:
        return t[s:e+1]
    return t


def repair_json(raw: str) -> Optional[Dict[str, Any]]:
    """Attempt to repair and parse potentially truncated JSON."""
    if not raw:
        return None
    cleaned = clean_json_text(raw)
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # Try closing open quotes and brackets
    for suffix in ['"}', '"]}', '}', '"]}', '"}}', 'null}']:
        try:
            return json.loads(cleaned + suffix)
        except Exception:
            continue

    # Regex extraction of action, sku, skus, reply
    try:
        act_m = re.search(r'"action"\s*:\s*"([A-Za-z_]+)"', raw)
        sku_m = re.search(r'"sku"\s*:\s*"([^"]+)"', raw)
        reply_m = re.search(r'"reply"\s*:\s*"([^"]+)', raw)
        skus_m = re.findall(r'"(GF:[^"]+)"', raw)
        if act_m or reply_m:
            return {
                "action": act_m.group(1) if act_m else "NONE",
                "sku": sku_m.group(1) if sku_m else None,
                "skus": skus_m,
                "reply": reply_m.group(1) if reply_m else "",
                "resume_prompt": "",
            }
    except Exception:
        pass
    return None


def build_action_frame(
    action_type: str,
    catalog_products: List[Dict[str, Any]],
    skus: List[str],
    sku: Optional[str],
    category_id: Optional[str],
    reply: str,
    filters_applied: Optional[Dict[str, Any]] = None,
    customer_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Build a structured UI action frame conforming to the frontend schema."""
    if action_type not in VALID_ACTIONS or action_type == "NONE":
        return None

    cat_by_sku = {p.get("sku"): p for p in catalog_products if p.get("sku")}

    if action_type == "SHOW_PRODUCTS":
        matched_prods = [cat_by_sku[s] for s in skus if s in cat_by_sku]
        if not matched_prods and category_id:
            matched_prods = [
                p for p in catalog_products
                if (p.get("category") or "").lower() == category_id.lower()
            ]
        if not matched_prods:
            matched_prods = catalog_products[:4]

        cards = [_build_product_card(p) for p in matched_prods[:6]]
        return {
            "action": "SHOW_PRODUCTS",
            "products": cards,
            "count": len(cards),
            "category": category_id,
            "message": reply,
            "filters_applied": filters_applied,
            "highlight_sku": cards[0]["sku"] if cards else None,
        }

    if action_type == "SHOW_PRODUCT":
        target = cat_by_sku.get(sku) if sku else None
        if not target and skus:
            target = cat_by_sku.get(skus[0])
        if not target:
            target = catalog_products[0] if catalog_products else None
        if not target:
            return None

        card = _build_product_card(target)
        return {
            "action": "SHOW_PRODUCT",
            "sku": card.get("sku"),
            "product": card,
            "message": reply,
        }

    if action_type == "SHOW_CATEGORIES":
        try:
            full_cat = get_verified_catalog()
            cats = catalog_categories(full_cat)
        except Exception:
            cats = []
        return {
            "action": "SHOW_CATEGORIES",
            "categories": cats,
            "show_all": True,
            "message": reply,
        }

    if action_type == "SHOW_ALL":
        cards = [_build_product_card(p) for p in catalog_products]
        return {
            "action": "SHOW_PRODUCTS",
            "products": cards,
            "count": len(cards),
            "category": "all",
            "message": reply,
        }

    if action_type == "SHOW_RECOMMENDATION":
        base_sku = sku or (skus[0] if skus else (catalog_products[0].get("sku") if catalog_products else ""))
        reco = build_recommendation_action(base_sku, customer_name)
        if reco:
            reco["message"] = reply or reco.get("message", "")
            return reco
        # Fallback to SHOW_PRODUCTS if recommendation engine has no match
        cards = [_build_product_card(p) for p in catalog_products[:3]]
        return {
            "action": "SHOW_PRODUCTS",
            "products": cards,
            "count": len(cards),
            "message": reply,
        }

    if action_type == "CONFIRM_BUY":
        target_sku = sku or (skus[0] if skus else "")
        target = cat_by_sku.get(target_sku) or (catalog_products[0] if catalog_products else {})
        return {
            "action": "CONFIRM_BUY",
            "sku": target.get("sku"),
            "product": _build_product_card(target) if target else None,
            "message": reply,
        }

    if action_type == "NAVIGATE_BACK":
        return {
            "action": "NAVIGATE_BACK",
            "message": reply,
        }

    return None


def quick_match_intent(
    customer_message: str,
    current_stage: str,
    current_context: Optional[Dict[str, Any]],
    all_products: List[Dict[str, Any]],
    customer_name: Optional[str] = None,
    current_screen: Optional[Dict[str, Any]] = None,
    last_list_skus: Optional[List[str]] = None,
) -> Optional[Tuple[Dict[str, Any], str, str, List[str]]]:
    """Deterministically match customer message to an instant action frame in <1ms.
    Returns (action_frame, cached_opener, action_type, skus) if a clear match is found, else None.
    """
    clean_msg = customer_message.strip()
    norm = normalize_query_text(clean_msg)
    msg_lower = clean_msg.lower()

    # Guard 1: On product page, questions like "is it microwave safe" or "dishwasher safe" return NONE (screen stays)
    is_property_q = bool(re.search(
        r"\b(microwave|dishwasher|safe|bpa|plastic|material|clean|wash|hot|reheat|food safe|durab)\b",
        msg_lower
    ))
    if current_screen and current_screen.get("type") == "product" and is_property_q:
        logger.info("[Match] On product screen and query is property question. Screen stays unchanged.")
        return None

    # Guard 2: Resolve references against the screen ("first/second/third one", "cheaper one", etc.)
    ref_sku = resolve_screen_reference(clean_msg, norm, last_list_skus, current_screen, all_products)
    if ref_sku:
        ref_prod = next((p for p in all_products if p.get("sku") == ref_sku), None)
        if ref_prod:
            opener = get_next_ack_opener()
            action_frame = build_action_frame(
                action_type="SHOW_PRODUCT",
                catalog_products=all_products,
                skus=[ref_sku],
                sku=ref_sku,
                category_id=ref_prod.get("category"),
                reply=opener,
                customer_name=customer_name,
            )
            if action_frame:
                return action_frame, opener, "SHOW_PRODUCT", [ref_sku]

    # Guard 3: Specific Product Match via Fuzzy Matcher (Evaluated BEFORE category keyword rules)
    scored = []
    for p in all_products:
        s = score_product_match(norm, p, clean_msg)
        if s > 0:
            scored.append((s, p))
    scored.sort(key=lambda x: x[0], reverse=True)

    if scored:
        top_score, top_prod = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        # If exactly one product is clearly best
        if top_score >= 0.60 and (top_score - second_score >= 0.15 or top_score >= 0.85):
            sku = top_prod.get("sku")
            opener = get_next_ack_opener()
            action_frame = build_action_frame(
                action_type="SHOW_PRODUCT",
                catalog_products=all_products,
                skus=[sku] if sku else [],
                sku=sku,
                category_id=top_prod.get("category"),
                reply=opener,
                customer_name=customer_name,
            )
            if action_frame:
                return action_frame, opener, "SHOW_PRODUCT", [sku] if sku else []

    # 1. Budget Intent
    budget_filter = parse_budget_intent(clean_msg)
    if budget_filter:
        budget_products, had_match = filter_catalog_by_budget(all_products, budget_filter)
        skus = [p.get("sku") for p in budget_products if p.get("sku")]
        opener = get_next_ack_opener()
        action_frame = build_action_frame(
            action_type="SHOW_PRODUCTS",
            catalog_products=all_products,
            skus=skus,
            sku=None,
            category_id=None,
            reply=opener,
            filters_applied=budget_filter,
            customer_name=customer_name,
        )
        if action_frame:
            return action_frame, opener, "SHOW_PRODUCTS", skus

    # 2. Gifting Intent
    if re.search(r"\b(gift|gifting|sister|brother|wedding|diwali|present|birthday)\b", msg_lower):
        gifting_prods = [
            p for p in all_products
            if any(tag in ((p.get("name") or "") + " " + (p.get("category") or "") + " " + (p.get("description") or "")).lower()
                   for tag in ["gift", "hamper", "set", "pack", "bundle", "mug"])
        ]
        if not gifting_prods:
            gifting_prods = all_products[:3]
        skus = [p.get("sku") for p in gifting_prods[:4] if p.get("sku")]
        opener = get_next_ack_opener()
        action_frame = build_action_frame(
            action_type="SHOW_PRODUCTS",
            catalog_products=all_products,
            skus=skus,
            sku=None,
            category_id="gifting",
            reply=opener,
            customer_name=customer_name,
        )
        if action_frame:
            return action_frame, opener, "SHOW_PRODUCTS", skus

    # 3. Bottles / School / Kids (when no single bottle was uniquely specified)
    if re.search(r"\b(bottle|bottles|school|kids|student)\b", msg_lower):
        bottle_prods = [
            p for p in all_products
            if "bottle" in ((p.get("name") or "") + " " + (p.get("category") or "")).lower() or
               "tumbler" in ((p.get("name") or "") + " " + (p.get("category") or "")).lower()
        ]
        if not bottle_prods:
            bottle_prods = all_products[:2]
        skus = [p.get("sku") for p in bottle_prods[:4] if p.get("sku")]
        opener = get_next_ack_opener()
        action_frame = build_action_frame(
            action_type="SHOW_PRODUCTS",
            catalog_products=all_products,
            skus=skus,
            sku=None,
            category_id="bottles",
            reply=opener,
            customer_name=customer_name,
        )
        if action_frame:
            return action_frame, opener, "SHOW_PRODUCTS", skus

    # 4. Recommendation / Pair-Up Intent
    if any(w in msg_lower for w in ["goes with", "pair up", "pair-up", "companion", "recommend"]):
        base_sku = (current_context or {}).get("selected_sku") or (all_products[0].get("sku") if all_products else "")
        opener = get_next_ack_opener()
        action_frame = build_action_frame(
            action_type="SHOW_RECOMMENDATION",
            catalog_products=all_products,
            skus=[base_sku] if base_sku else [],
            sku=base_sku,
            category_id=None,
            reply=opener,
            customer_name=customer_name,
        )
        if action_frame:
            return action_frame, opener, "SHOW_RECOMMENDATION", [base_sku] if base_sku else []

    # 5. Storage Intent
    if re.search(r"\b(storage|canister|containers?|canisters?|organizer)\b", msg_lower):
        storage_prods = [
            p for p in all_products
            if any(w in ((p.get("name") or "") + " " + (p.get("category") or "") + " " + (p.get("description") or "")).lower()
                   for w in ["storage", "canister"])
        ]
        if not storage_prods:
            storage_prods = all_products[:3]
        skus = [p.get("sku") for p in storage_prods if p.get("sku")]
        opener = get_next_ack_opener()
        action_frame = build_action_frame(
            action_type="SHOW_PRODUCTS",
            catalog_products=all_products,
            skus=skus,
            sku=None,
            category_id="storage",
            reply=opener,
            customer_name=customer_name,
        )
        if action_frame:
            return action_frame, opener, "SHOW_PRODUCTS", skus

    # 6. Show More Products / Browse All / Show Products / More Products (NEVER SHOW_CATEGORIES)
    if re.search(r"\b(show (?:me )?more products|more products|show (?:me )?products|browse all|all products|full collection|explore all)\b", msg_lower):
        opener = get_next_ack_opener()
        cat_id = None
        if current_screen and current_screen.get("type") == "product":
            cat_id = current_screen.get("category")
        elif current_screen and current_screen.get("type") == "categories":
            cat_id = "Drinkware"

        filtered = [p for p in all_products if (p.get("category") or "").lower() == (cat_id or "").lower()] if cat_id else all_products
        if not filtered:
            filtered = all_products
        skus = [p.get("sku") for p in filtered if p.get("sku")]
        action_frame = build_action_frame(
            action_type="SHOW_PRODUCTS",
            catalog_products=all_products,
            skus=skus,
            sku=None,
            category_id=cat_id or "all",
            reply=opener,
            customer_name=customer_name,
        )
        if action_frame:
            return action_frame, opener, "SHOW_PRODUCTS", skus

    # 7. Show Categories (Dedupe: Never send SHOW_CATEGORIES when already on categories, and never after onboarding unless explicitly asking for categories)
    has_explicit_categories = bool(re.search(r"\b(categories|category)\b", msg_lower))
    is_onboarding_phrase = bool(re.search(r"\b(skip|skip name|no name|just browse)\b", msg_lower))
    name_status = (current_context or {}).get("name_status", "unknown")
    onboarding_done = name_status in ("given", "skipped") or (current_screen and current_screen.get("type") in ("list", "product", "cart", "categories"))

    if has_explicit_categories or (is_onboarding_phrase and not onboarding_done):
        if current_screen and current_screen.get("type") == "categories":
            logger.info("[Match] Already on categories screen. Skipping duplicate SHOW_CATEGORIES.")
            return None
        opener = get_next_ack_opener()
        action_frame = build_action_frame(
            action_type="SHOW_CATEGORIES",
            catalog_products=all_products,
            skus=[],
            sku=None,
            category_id=None,
            reply=opener,
            customer_name=customer_name,
        )
        if action_frame:
            return action_frame, opener, "SHOW_CATEGORIES", []

    # 8. Buy / Add to Cart Intent
    if re.search(r"\b(buy (?:this|it)?|add to cart|add this to (?:my )?cart|take this|i'll take it|ill take it|purchase)\b", msg_lower):
        target_sku = (current_screen or {}).get("sku") or (current_context or {}).get("selected_sku")
        if target_sku:
            opener = get_next_ack_opener()
            action_frame = build_action_frame(
                action_type="CONFIRM_BUY",
                catalog_products=all_products,
                skus=[target_sku],
                sku=target_sku,
                category_id=None,
                reply=opener,
                customer_name=customer_name,
            )
            if action_frame:
                return action_frame, opener, "CONFIRM_BUY", [target_sku]

    return None


class StreamingJsonReplyParser:
    """State-machine streaming JSON extractor.
    Extracts only the string value of the 'reply' key while streaming, emitting tokens
    and sentences at punctuation boundaries (. ! ? ।).
    Stops emitting tokens and sentences at the closing quote of 'reply'.
    Buffers all remaining fields (action, category_id, skus, sku, filters, resume_prompt)
    and parses them with json.loads after the stream completes.
    """
    def __init__(self):
        self.raw_stream = ""
        # 0: searching for "reply", 1: searching for :, 2: searching for opening quote ",
        # 3: inside reply string value, 4: reply string closed (finished)
        self.state = 0
        self.escape = False
        self.unicode_escape: Optional[List[str]] = None
        self.reply_extracted = ""
        self.sentence_buffer = ""
        self.completed_sentences: List[str] = []

    def feed(self, delta: str) -> Tuple[List[str], str]:
        """Feed a delta string from LLM stream.
        Returns (new_sentences, reply_tokens_delta).
        """
        self.raw_stream += delta
        new_sentences: List[str] = []
        token_chars: List[str] = []

        for c in delta:
            if self.state == 0:
                idx = self.raw_stream.find('"reply"')
                if idx != -1:
                    self.state = 1
                continue

            if self.state == 1:
                if c == ':':
                    self.state = 2
                continue

            if self.state == 2:
                if c == '"':
                    self.state = 3
                continue

            if self.state == 3:
                # Accumulate 4-digit unicode hex escape e.g. \u20b9
                if self.unicode_escape is not None:
                    self.unicode_escape.append(c)
                    if len(self.unicode_escape) == 4:
                        hex_str = "".join(self.unicode_escape)
                        try:
                            ch = chr(int(hex_str, 16))
                        except Exception:
                            ch = ""
                        self.unicode_escape = None
                        if ch:
                            token_chars.append(ch)
                            self.sentence_buffer += ch
                            self.reply_extracted += ch
                    continue

                if self.escape:
                    self.escape = False
                    if c == 'n':
                        ch = ' '
                    elif c == 't':
                        ch = ' '
                    elif c == 'r':
                        ch = ''
                    elif c == 'u':
                        self.unicode_escape = []
                        continue
                    elif c == '"':
                        ch = '"'
                    elif c == '\\':
                        ch = '\\'
                    else:
                        ch = c
                    if ch:
                        token_chars.append(ch)
                        self.sentence_buffer += ch
                        self.reply_extracted += ch
                    continue

                if c == '\\':
                    self.escape = True
                    continue

                if c == '"':
                    # Closing quote of "reply"! Stop sending tokens and sentences immediately.
                    self.state = 4
                    last_sent = sanitize_spoken_text(self.sentence_buffer.strip())
                    if last_sent and last_sent not in self.completed_sentences:
                        if not any(k in last_sent for k in ('"reply"', '"action"', '"skus"', '{', '}')):
                            new_sentences.append(last_sent)
                            self.completed_sentences.append(last_sent)
                        else:
                            logger.warning("[Chat] dropped non-speech text: %s", last_sent)
                    self.sentence_buffer = ""
                    break

                token_chars.append(c)
                self.sentence_buffer += c
                self.reply_extracted += c

                if c in ('.', '!', '?', '\u0964'):
                    s = sanitize_spoken_text(self.sentence_buffer.strip())
                    if len(s) > 3 and not re.search(r'\b(?:rs|inr|no|mr|ms|dr|vs|approx)\.$', s, re.IGNORECASE):
                        if not any(k in s for k in ('"reply"', '"action"', '"skus"', '{', '}')):
                            new_sentences.append(s)
                            self.completed_sentences.append(s)
                        else:
                            logger.warning("[Chat] dropped non-speech text: %s", s)
                        self.sentence_buffer = ""

            if self.state == 4:
                # Reply is completely closed. No tokens or sentences will ever be emitted for rest of JSON.
                break

        reply_tokens_delta = "".join(token_chars)
        return new_sentences, reply_tokens_delta

    def finish(
        self,
        customer_message: str = "",
        current_stage: str = "BROWSING",
        current_context: Optional[Dict[str, Any]] = None,
        all_products: Optional[List[Dict[str, Any]]] = None,
        customer_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Finalize parsing after LLM stream completes.
        Parses action, category_id, skus, sku, filters, resume_prompt without TTS sanitizing them.
        Falls back to code fences strip, regex extraction, or deterministic matcher if JSON parse fails.
        """
        last_sent = sanitize_spoken_text(self.sentence_buffer.strip())
        if last_sent and last_sent not in self.completed_sentences:
            if not any(k in last_sent for k in ('"reply"', '"action"', '"skus"', '{', '}')):
                self.completed_sentences.append(last_sent)
            else:
                logger.warning("[Chat] dropped non-speech text: %s", last_sent)
        self.sentence_buffer = ""

        raw = self.raw_stream.strip()
        parsed: Optional[Dict[str, Any]] = None

        # 1. Parse or repair JSON
        parsed = repair_json(raw)

        # 2. Fallback regex extraction of fields
        if not isinstance(parsed, dict):
            parsed = {}
            m_act = re.search(r'"action"\s*:\s*"([^"]+)"', raw, re.IGNORECASE)
            if m_act:
                parsed["action"] = m_act.group(1)
            m_cat = re.search(r'"category_id"\s*:\s*"([^"]+)"', raw, re.IGNORECASE)
            if m_cat:
                parsed["category_id"] = m_cat.group(1)
            m_sku = re.search(r'"sku"\s*:\s*"([^"]+)"', raw, re.IGNORECASE)
            if m_sku:
                parsed["sku"] = m_sku.group(1)
            m_skus = re.search(r'"skus"\s*:\s*\[(.*?)\]', raw, re.DOTALL)
            if m_skus:
                skus_raw = m_skus.group(1)
                parsed["skus"] = [s.strip().strip('"').strip("'") for s in skus_raw.split(",") if s.strip().strip('"').strip("'")]
            m_rp = re.search(r'"resume_prompt"\s*:\s*"([^"]+)"', raw, re.IGNORECASE)
            if m_rp:
                parsed["resume_prompt"] = m_rp.group(1)

        # 3. Fallback to deterministic code matcher if action is missing or invalid
        raw_action = str(parsed.get("action") or "").strip()
        norm_action = re.sub(r"[\s-]+", "_", raw_action).upper()
        if (not norm_action or norm_action not in VALID_ACTIONS or norm_action == "NONE" or (norm_action == "SHOW_PRODUCTS" and not parsed.get("skus"))) and customer_message and all_products:
            qm = quick_match_intent(
                customer_message=customer_message,
                current_stage=current_stage,
                current_context=current_context,
                all_products=all_products,
                customer_name=customer_name,
                current_screen=current_context.get("current_screen") if current_context else None,
                last_list_skus=current_context.get("last_list_skus") if current_context else None,
            )
            if qm:
                qm_frame, qm_opener, qm_action, qm_skus = qm
                parsed["action"] = qm_action
                if not parsed.get("skus"):
                    parsed["skus"] = qm_skus
                if qm_action == "SHOW_PRODUCT" and qm_skus:
                    parsed["sku"] = qm_skus[0]

        # Ensure reply string is present and clean
        if not parsed.get("reply"):
            parsed["reply"] = self.reply_extracted or " ".join(self.completed_sentences)

        return parsed


_async_groq_client: Optional[Any] = None
_groq_http_client: Optional[Any] = None


def get_async_groq_client():
    """Return singleton AsyncGroq client with persistent HTTP connection pool."""
    global _async_groq_client, _groq_http_client
    if _async_groq_client is None:
        import httpx
        from groq import AsyncGroq
        _groq_http_client = httpx.AsyncClient(
            limits=httpx.Limits(max_keepalive_connections=16, max_connections=32, keepalive_expiry=60.0),
            timeout=httpx.Timeout(8.0, connect=3.0),
        )
        _async_groq_client = AsyncGroq(
            api_key=settings.groq_api_key,
            http_client=_groq_http_client,
        )
    return _async_groq_client


async def warmup_conversation_agent():
    """Send a tiny 1-token warm-up request to establish TLS keep-alive connections on connect."""
    try:
        client = get_async_groq_client()
        await client.chat.completions.create(
            model=settings.groq_model_name,
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=1,
            temperature=0.0,
        )
        logger.info("[Chat] Groq keep-alive client warmed up.")
    except Exception as exc:
        logger.warning("[Chat] Groq warm-up non-critical error: %s", exc)


def _build_fallback_response(
    clean_msg: str,
    all_products: List[Dict[str, Any]],
    budget_filter: Optional[Dict[str, Any]] = None,
    budget_products: Optional[List[Dict[str, Any]]] = None,
    had_budget_match: bool = False,
    customer_name: Optional[str] = None,
    current_screen: Optional[Dict[str, Any]] = None,
    categories_shown_at: float = 0.0,
    name_status: str = "unknown",
    name_asked_count: int = 0,
) -> Dict[str, Any]:
    """Deterministic instant fallback with state-aware screen description and category suppression."""
    msg_l = clean_msg.lower()
    screen_info = current_screen or {"type": "home"}
    screen_type = screen_info.get("type", "home")

    can_ask_name = (
        name_status == "unknown"
        and name_asked_count == 0
        and screen_type in ("home", "greeting")
    )
    resume_prompt = "Before we start, may I know your name?" if can_ask_name else ""

    log_reason = "general_fallback"

    if budget_filter:
        log_reason = "budget_fallback"
        if had_budget_match and budget_products:
            top_p = budget_products[0]
            reply_text = f"I found {len(budget_products)} lovely options under your budget. The {top_p.get('name')} is a great pick at {int(float(top_p.get('price', 0)))} rupees."
            act = "SHOW_PRODUCTS"
            skus_list = [p.get("sku") for p in budget_products if p.get("sku")]
        else:
            top_p = budget_products[0] if budget_products else None
            act = "SHOW_PRODUCTS"
            skus_list = [top_p.get("sku")] if top_p else []
            reply_text = f"We do not have items under {int(budget_filter.get('max_price', 0))} rupees, but our closest option is the {top_p.get('name')} at {int(float(top_p.get('price', 0)))} rupees." if top_p else "We do not have items in that budget right now."

    elif re.search(r"\b(microwave|dishwasher|safe|material|plastic|bpa)\b", msg_l):
        log_reason = "tableware_safety_query"
        act = "NONE"
        skus_list = []
        reply_text = "Yes, all our Green Fibre tableware is microwave-reheat safe and dishwasher safe, made from upcycled rice husk with zero virgin plastic."

    elif screen_type == "product":
        log_reason = "on_product_screen"
        act = "NONE"
        skus_list = []
        prod_sku = screen_info.get("sku")
        matched_p = next((p for p in all_products if p.get("sku") == prod_sku), None)
        if matched_p:
            from app.product_speech import answer_product_question
            reply_text = answer_product_question(matched_p, clean_msg)
        else:
            reply_text = "This eco-friendly piece is crafted from upcycled rice husk biocomposite. Let me know if you'd like to see other options."

    elif screen_type == "list":
        log_reason = "on_list_screen"
        act = "NONE"
        skus_list = []
        cat = screen_info.get("category")
        clean_cat = (cat or "").replace("our ", "").strip()
        cat_str = f"our {clean_cat}" if clean_cat and clean_cat != "all" else "our collection"
        reply_text = f"Here are the items in {cat_str}. Tap any item on the screen to view details, or ask me anything."

    elif screen_type == "categories":
        log_reason = "already_on_categories"
        act = "NONE"
        skus_list = []
        reply_text = "Browse our categories on screen, or tell me what you need and I'll find it for you."

    elif screen_type == "home":
        # Home screen: check 60s rule
        time_since = time.time() - categories_shown_at
        if time_since < 60.0 and categories_shown_at > 0:
            log_reason = "home_categories_recently_shown"
            act = "NONE"
            skus_list = []
            reply_text = "Take a look around our collection, or let me know what you'd like to explore."
        else:
            log_reason = "home_show_categories"
            act = "SHOW_CATEGORIES"
            skus_list = []
            reply_text = "What are you looking for today? Select a category or tell me what you need."

    else:
        log_reason = "default_screen_fallback"
        act = "NONE"
        skus_list = []
        reply_text = "I'm here to help you find the perfect eco-friendly pieces. Let me know what you're looking for!"

    # Ensure never sending SHOW_CATEGORIES if already on categories
    if act == "SHOW_CATEGORIES" and screen_type == "categories":
        act = "NONE"
        log_reason += "_deduped_categories"

    logger.info("[Fallback] reason=%s reply=%s action=%s", log_reason, reply_text, act)
    print(f"[Fallback] reason={log_reason} reply={reply_text} action={act}", flush=True)

    return {
        "reply": reply_text,
        "action": act,
        "skus": skus_list,
        "resume_prompt": resume_prompt,
    }


async def stream_free_conversation(
    session_id: str,
    customer_message: str,
    current_stage: str = "BROWSING",
    customer_name: Optional[str] = None,
    current_context: Optional[Dict[str, Any]] = None,
    history: Optional[List[Dict[str, str]]] = None,
    turn_id: Optional[str] = None,
    all_products: Optional[List[Dict[str, Any]]] = None,
    compact_catalog: Optional[List[Dict[str, Any]]] = None,
    instant_match: bool = False,
    expected_screen_action: Optional[Dict[str, Any]] = None,
    name_status: str = "unknown",
    name_asked_count: int = 0,
    current_screen: Optional[Dict[str, Any]] = None,
    categories_shown_at: float = 0.0,
):
    """Real LLM streaming conversation handler emitting tokens, completed sentences, and actions."""
    clean_msg = customer_message.strip()

    if all_products is None or compact_catalog is None:
        try:
            catalog = get_verified_catalog()
        except Exception:
            catalog = {"products": []}
        all_products = catalog.get("products", [])
        compact_catalog = build_compact_catalog(catalog)

    budget_filter = parse_budget_intent(clean_msg)
    budget_products: List[Dict[str, Any]] = []
    had_budget_match = False
    if budget_filter:
        budget_products, had_budget_match = filter_catalog_by_budget(all_products, budget_filter)

    sys_prompt = build_system_prompt(
        compact_catalog=compact_catalog,
        current_stage=current_stage,
        customer_name=customer_name,
        current_context=current_context,
        budget_intent=budget_filter,
        budget_products=budget_products,
        had_budget_match=had_budget_match,
        expected_screen_action=expected_screen_action,
        name_status=name_status,
        name_asked_count=name_asked_count,
        current_screen=current_screen,
    )

    llm_messages = [{"role": "system", "content": sys_prompt}]
    if history:
        for turn in history[-4:]:
            r = turn.get("role")
            c = turn.get("content", "")
            if r in ("user", "assistant"):
                llm_messages.append({"role": r, "content": c})
    llm_messages.append({"role": "user", "content": clean_msg})

    parser = StreamingJsonReplyParser()
    t_start = time.perf_counter()
    first_token_sent = False

    try:
        client = get_async_groq_client()
        stream = await client.chat.completions.create(
            model=settings.groq_model_name,
            messages=llm_messages,
            temperature=0.15,
            max_tokens=200,
            stream=True,
        )

        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                if not first_token_sent:
                    first_token_sent = True
                    yield "llm_first_token", int((time.perf_counter() - t_start) * 1000)

                sentences, reply_token_delta = parser.feed(delta)
                if reply_token_delta:
                    yield "token", reply_token_delta
                for sent in sentences:
                    if any(k in sent for k in ('"reply"', '"action"', '"skus"', '{', '}')):
                        logger.warning("[Chat] dropped non-speech text: %s", sent)
                        continue
                    yield "sentence", sent

        t_done = time.perf_counter()
        yield "llm_done", int((t_done - t_start) * 1000)
        parsed_json = parser.finish(
            customer_message=clean_msg,
            current_stage=current_stage,
            current_context=current_context,
            all_products=all_products,
            customer_name=customer_name,
        )

    except Exception as exc:
        logger.warning("[Chat] Groq streaming error: %s, using fallback", exc)
        t_done = time.perf_counter()
        yield "llm_done", int((t_done - t_start) * 1000)
        parsed_json = None

    if not parsed_json or not isinstance(parsed_json, dict) or not parsed_json.get("reply"):
        parsed_json = _build_fallback_response(
            clean_msg=clean_msg,
            all_products=all_products,
            budget_filter=budget_filter,
            budget_products=budget_products,
            had_budget_match=had_budget_match,
            customer_name=customer_name,
            current_screen=current_screen,
            categories_shown_at=categories_shown_at,
            name_status=name_status,
            name_asked_count=name_asked_count,
        )
        fallback_reply = parsed_json.get("reply", "")
        if not parser.completed_sentences:
            for s in fallback_reply.split(". "):
                clean_s = s.strip()
                if clean_s:
                    if any(k in clean_s for k in ('"reply"', '"action"', '"skus"', '{', '}')):
                        logger.warning("[Chat] dropped non-speech text: %s", clean_s)
                        continue
                    yield "sentence", clean_s

    reply = sanitize_spoken_text(str(parsed_json.get("reply") or ""))

    # Normalise action names so "SHOW PRODUCTS", "show_products" and "SHOW_PRODUCTS" all map to SHOW_PRODUCTS
    raw_action = str(parsed_json.get("action") or "NONE")
    action_type = re.sub(r"[\s-]+", "_", raw_action).strip().upper()
    if action_type not in VALID_ACTIONS:
        action_type = "NONE"
    if action_type == "SHOW_CATEGORIES" and current_screen and current_screen.get("type") == "categories":
        action_type = "NONE"

    # Validate skus that exist in catalog
    cat_skus = {p.get("sku") for p in all_products if p.get("sku")}
    raw_skus = parsed_json.get("skus") or []
    valid_skus = [str(s) for s in raw_skus if str(s) in cat_skus]

    sku = parsed_json.get("sku")
    if sku and str(sku) not in cat_skus:
        sku = valid_skus[0] if valid_skus else None

    category_id = parsed_json.get("category_id")
    resume_prompt = sanitize_spoken_text(str(parsed_json.get("resume_prompt") or ""))

    filters_applied = parsed_json.get("filters")
    if budget_filter:
        action_type = "SHOW_PRODUCTS"
        valid_skus = [p.get("sku") for p in budget_products if p.get("sku")]
        filters_applied = budget_filter

    action_frame = build_action_frame(
        action_type=action_type,
        catalog_products=all_products,
        skus=valid_skus,
        sku=sku,
        category_id=category_id,
        reply=reply,
        filters_applied=filters_applied,
        customer_name=customer_name,
    )

    if action_frame:
        frame_action = action_frame.get("action")
        frame_skus = [p.get("sku") for p in action_frame.get("products", [])] if action_frame.get("products") else ([action_frame.get("sku")] if action_frame.get("sku") else valid_skus)
        logger.info("[Chat] frame_sent action=%s skus=%s", frame_action, frame_skus)
        print(f"[Chat] frame_sent action={frame_action} skus={frame_skus}", flush=True)
        yield "action", action_frame

    yield "done", {
        "reply": reply,
        "action": action_frame,
        "sales_stage": action_type if action_type != "NONE" else current_stage,
        "resume_prompt": resume_prompt,
        "skus": valid_skus,
        "filters": filters_applied,
    }


def handle_free_conversation(
    session_id: str,
    customer_message: str,
    current_stage: str = "BROWSING",
    customer_name: Optional[str] = None,
    current_context: Optional[Dict[str, Any]] = None,
    history: Optional[List[Dict[str, str]]] = None,
    turn_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute free conversation understanding with strict JSON output, deterministic budget, and UI action."""
    start_time = time.time()
    clean_msg = customer_message.strip()
    cache_key = clean_msg.lower()

    # 1. Check TTL cache
    now = time.time()
    if cache_key in _CONV_CACHE:
        cached_ts, cached_res = _CONV_CACHE[cache_key]
        if now - cached_ts < CACHE_TTL_SECONDS:
            logger.info("[Chat] Cache hit for '%s'", clean_msg)
            return dict(cached_res)

    # 2. Load catalog
    try:
        catalog = get_verified_catalog()
    except Exception as exc:
        logger.warning("[Chat] Failed to load catalog: %s", exc)
        catalog = {"products": []}

    all_products = catalog.get("products", [])
    compact_catalog = build_compact_catalog(catalog)

    # 3. Deterministic Budget Detection
    budget_filter = parse_budget_intent(clean_msg)
    budget_products: List[Dict[str, Any]] = []
    had_budget_match = False
    if budget_filter:
        budget_products, had_budget_match = filter_catalog_by_budget(all_products, budget_filter)

    # 4. Prompt Assembly
    sys_prompt = build_system_prompt(
        compact_catalog=compact_catalog,
        current_stage=current_stage,
        customer_name=customer_name,
        current_context=current_context,
        budget_intent=budget_filter,
        budget_products=budget_products,
        had_budget_match=had_budget_match,
    )

    from app.brain import get_llm
    from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

    messages = [SystemMessage(content=sys_prompt)]
    if history:
        for turn in history[-6:]:
            role = turn.get("role")
            content = turn.get("content", "")
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))

    messages.append(HumanMessage(content=clean_msg))

    llm = get_llm()
    raw_content = ""
    parsed_json: Optional[Dict[str, Any]] = None

    for attempt in range(2):
        try:
            resp = llm.invoke(messages)
            raw_content = str(resp.content)
            cleaned = clean_json_text(raw_content)
            parsed_json = json.loads(cleaned)
            break
        except Exception as err:
            logger.warning("[Chat] JSON parse attempt %d failed: %s (raw='%s')", attempt + 1, err, raw_content[:100])
            if attempt == 0:
                messages.append(AIMessage(content=raw_content))
                messages.append(HumanMessage(content="Your response was not valid JSON. Please output ONLY the raw JSON object matching the required schema."))

    # 5. Fallback if JSON invalid after retry
    if not isinstance(parsed_json, dict) or not parsed_json.get("reply"):
        logger.warning("[Chat] fallback reason=json_invalid_or_rate_limit")
        parsed_json = _build_fallback_response(
            clean_msg=clean_msg,
            all_products=all_products,
            budget_filter=budget_filter,
            budget_products=budget_products,
            had_budget_match=had_budget_match,
            customer_name=customer_name,
        )

    reply = sanitize_spoken_text(str(parsed_json.get("reply") or ""))
    action_type = str(parsed_json.get("action") or "NONE").upper().strip()
    if action_type not in VALID_ACTIONS:
        action_type = "NONE"

    skus = [str(s) for s in (parsed_json.get("skus") or []) if s]
    sku = parsed_json.get("sku")
    category_id = parsed_json.get("category_id")
    resume_prompt = sanitize_spoken_text(str(parsed_json.get("resume_prompt") or ""))

    # 6. Override with Deterministic Budget Results if budget was specified
    filters_applied = parsed_json.get("filters")
    if budget_filter:
        action_type = "SHOW_PRODUCTS"
        skus = [p.get("sku") for p in budget_products if p.get("sku")]
        filters_applied = {
            "max_price": budget_filter.get("max_price"),
            "min_price": budget_filter.get("min_price"),
            "label": budget_filter.get("label"),
        }

    # 7. Check for recommendations intent
    if any(w in clean_msg.lower() for w in ["goes with this", "pair up", "pair-up", "companion", "recommendation"]):
        action_type = "SHOW_RECOMMENDATION"

    # 8. Check for specific product match if action is SHOW_PRODUCT or mentioned
    if action_type in ("SHOW_PRODUCT", "NONE"):
        prod_matched = find_product_by_text(clean_msg, all_products)
        if prod_matched and action_type == "NONE" and any(w in clean_msg.lower() for w in ["tell me about", "show", "details", "info"]):
            action_type = "SHOW_PRODUCT"
            sku = prod_matched.get("sku")
        elif prod_matched and action_type == "SHOW_PRODUCT" and not sku:
            sku = prod_matched.get("sku")

    # 9. Mid-flow and off-topic resume prompt handling
    # 9. Mid-flow and off-topic resume prompt handling
    if not customer_name and current_stage in ("NAME", "WAKE"):
        if not resume_prompt:
            resume_prompt = "Before we explore, may I know your name?"
        if not re.search(r"\bname\b", reply, re.IGNORECASE) and not reply.endswith("?"):
            reply = f"{reply} {resume_prompt}".strip()
    elif action_type == "NONE":
        # Only append a resume prompt if reply does not already ask a question
        if "?" not in reply and resume_prompt and resume_prompt.lower() not in reply.lower():
            reply = f"{reply} {resume_prompt}".strip()

    # 10. Build full UI Action Frame
    action_frame = build_action_frame(
        action_type=action_type,
        catalog_products=all_products,
        skus=skus,
        sku=sku,
        category_id=category_id,
        reply=reply,
        filters_applied=filters_applied,
        customer_name=customer_name,
    )

    latency_ms = int((time.time() - start_time) * 1000)
    logger.info(
        "[Chat] msg='%s' stage=%s action=%s skus=%s filters=%s latency_ms=%d",
        clean_msg, current_stage, action_type, skus, filters_applied, latency_ms
    )

    res = {
        "reply": reply,
        "action": action_frame,
        "sales_stage": action_type if action_type != "NONE" else current_stage,
        "resume_prompt": resume_prompt,
        "skus": skus,
        "filters": filters_applied,
    }

    # Save to TTL cache
    _CONV_CACHE[cache_key] = (now, res)

    return res


# ── Small Talk & Out-Of-Context Intent Handler ──────────────────────────────

_SMALL_TALK_ROTATION = 0

def match_small_talk_intent(
    customer_message: str,
    customer_name: Optional[str] = None,
    all_products: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """Determine if customer message is friendly small talk or out-of-context.
    Used ONLY when speech is NOT a product, category, cart, or navigation command.
    Returns dict with {intent, reply} if matched, else None.
    """
    global _SMALL_TALK_ROTATION
    raw = (customer_message or "").strip()
    msg_l = raw.lower()
    norm = re.sub(r"[^\w\s]", " ", msg_l).strip()
    norm = re.sub(r"\s+", " ", norm)

    # User's name injection (at most once per reply)
    name_clause = f", {customer_name}" if customer_name else ""

    # Rotation index (0, 1, 2)
    variant_idx = _SMALL_TALK_ROTATION % 3
    _SMALL_TALK_ROTATION += 1

    # Intent 7: Speech not understood (empty, very short, noise, filler)
    if not norm or len(norm) <= 2 or re.match(r"^(?:uh|um|ah|hmm|mhm|er|eh|noise|speech|test)$", norm):
        replies = [
            "Sorry, I couldn't catch that, it may be some noise around. Could you say it again, or tap on the screen?",
            "Pardon me, there seems to be a little background noise. Could you repeat that or tap on the display?",
            "I didn't quite catch that. Would you mind repeating, or you can tap any category to explore!",
        ]
        return {"intent": "not_understood", "reply": replies[variant_idx]}

    # Intent 1: Greetings and "how are you"
    if re.search(r"\b(how are you|how r u|how are things|how do you do|how's it going|how are you doing|how is your day|good morning|good afternoon|good evening|hello greeny|hi greeny)\b", norm) or norm in ("hello", "hi", "hey"):
        replies = [
            f"I'm doing great, thank you for asking{name_clause}! How can I help you today?",
            f"I'm having a wonderful day{name_clause}, thank you! What can I help you find today?",
            f"Doing very well, thank you{name_clause}! What would you like to explore today?",
        ]
        return {"intent": "greeting", "reply": replies[variant_idx]}

    # Intent 2: Thanks
    if re.search(r"\b(thank you|thanks|thank u|thanks a lot|thank you so much|thx|dhanyawad|shukriya)\b", norm):
        replies = [
            f"You're most welcome{name_clause}! I'm right here if you need anything.",
            f"Always happy to help{name_clause}! Let me know if you'd like to see more items.",
            f"Anytime{name_clause}! Feel free to tap the screen or ask me about any product.",
        ]
        return {"intent": "thanks", "reply": replies[variant_idx]}

    # Intent 3: Who are you / what can you do
    if re.search(r"\b(who are you|what are you|what can you do|your name|what do you do|tell me about yourself|who is greeny)\b", norm):
        replies = [
            "I'm Greeny, your shopping guide. I can show you products, prices and colours, just ask me.",
            "I'm Greeny, your store assistant. You can ask me about our sustainable collection, prices, or tap the screen to explore.",
            "I'm Greeny! I help you find eco-friendly tableware, bottles and gifts. Tell me what you need, or tap any category.",
        ]
        return {"intent": "identity", "reply": replies[variant_idx]}

    # Intent 4: Goodbye
    if re.search(r"\b(goodbye|good bye|bye|see you|bye bye|see ya|alvida)\b", norm):
        replies = [
            f"Thank you for visiting Green Fibre{name_clause}! Have a lovely day.",
            f"Goodbye{name_clause}! Thank you for stopping by Green Fibre. Have a wonderful day ahead.",
            f"Take care{name_clause}! Thank you for exploring Green Fibre. See you soon.",
        ]
        return {"intent": "goodbye", "reply": replies[variant_idx]}

    # Intent 6: Unrelated general question (weather, news, politics, cricket, jokes, etc.)
    if re.search(r"\b(weather|temperature|forecast|rain|sunny|climate|news|politics|election|minister|president|cricket|score|match|ipl|joke|jokes|tell me a joke|capital of|time is it|what time|stock market|bitcoin)\b", norm):
        replies = [
            "I'm best at helping with our eco-friendly products. What are you looking for today?",
            "I focus on our sustainable lifestyle collection here at GreenFibre. What can I help you find today?",
            "I'm here to help you shop our sustainable essentials! Tell me what you'd like to explore.",
        ]
        return {"intent": "general_out_of_context", "reply": replies[variant_idx]}

    # Intent 5: Product or category name that does NOT exist in the catalogue
    unlisted_item_words = (
        "laptop", "computer", "notebook", "pc", "macbook",
        "shoe", "shoes", "sneaker", "sneakers", "sandals", "slippers",
        "phone", "mobile", "iphone", "smartphone", "android",
        "tv", "television", "headphone", "headphones", "earbuds",
        "clothes", "clothing", "shirt", "tshirt", "t-shirt", "pant", "jeans", "dress", "saree",
        "watch", "smartwatch", "jewelry", "bag", "handbag", "purse",
        "book", "books", "furniture", "sofa", "bed", "chair", "mattress",
        "perfume", "cosmetics", "makeup", "toy", "toys", "groceries", "food", "pizza",
    )
    is_unlisted_query = any(re.search(r"\b" + re.escape(w) + r"\b", norm) for w in unlisted_item_words)
    has_shopping_syntax = bool(re.search(r"\b(show me|looking for|want|need|have|where is|buy|search)\b", norm))

    if is_unlisted_query or (has_shopping_syntax and not any(kw in norm for kw in ("bottle", "mug", "bowl", "casserole", "planter", "canister", "gift", "dining", "desk", "tissue", "storage", "green fibre", "rice husk"))):
        replies = [
            "I couldn't find that here, but you can browse our categories on screen, or tell me what you need and I'll suggest something.",
            "We don't have that in our collection, but feel free to explore our sustainable kitchen and drinkware on screen!",
            "I don't see that item in our catalogue, but tell me what you're looking for and I'll find our closest eco-match.",
        ]
        return {"intent": "unlisted_product", "reply": replies[variant_idx]}

    return None
