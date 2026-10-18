"""Product speech generator for Greeny avatar on the product details screen.
Generates natural, short, friendly chunks (~10-12s, max 3 chunks) from real product data.
"""

from __future__ import annotations
import re
from typing import Any, Dict, List, Optional, Tuple


def format_rupees(num: Any) -> str:
    try:
        n = round(float(num or 0))
    except Exception:
        n = 0
    return f"{n:,} rupees" if n > 0 else ""


def clean_product_name(name: Optional[str]) -> str:
    if not name or not isinstance(name, str):
        return "this product"
    clean = re.sub(r"\s*[–—\-]\s*(?:set\s+of\s+\d+|pair\s+of\s+\d+)", lambda m: ", " + re.sub(r"\s*[–—\-]\s*", "", m.group(0)).lower(), name, flags=re.IGNORECASE)
    clean = re.sub(r"\s*[–—\-]\s*\d+\s*(?:ml|inch|l|litres?|grams?|g)\b", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\s+[–—\-]\s+", ", ", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or "this product"


def extract_product_colours(product: Dict[str, Any]) -> List[str]:
    variants = product.get("variants") or []
    colours: List[str] = []
    placeholder_regex = re.compile(
        r"^(?:multiple\s*colou?rs?|mixed\s*/?\s*product\s*assortment|product\s*assortment|mixed|assortment|natural|default|none)$",
        re.IGNORECASE
    )

    for v in variants:
        if not isinstance(v, dict):
            continue
        raw = (v.get("colour") or v.get("color") or v.get("name") or "").strip()
        if raw and not placeholder_regex.match(raw) and raw not in colours:
            colours.append(raw)
    return colours


def extract_key_feature(product: Dict[str, Any]) -> str:
    desc = str(product.get("description") or "").lower()
    if "stainless steel" in desc:
        return "made with a stainless steel inner and natural rice husk"
    if "bamboo" in desc:
        return "crafted from eco-friendly bamboo and crop fibres"
    return "made from natural rice husk"


def normalize_for_speech(text: str) -> str:
    if not text:
        return ""
    t = str(text)

    def _repl_rupees(m):
        raw = m.group(1).replace(",", "")
        try:
            n = round(float(raw))
            return f"{n:,} rupees " if n > 0 else ""
        except Exception:
            return m.group(0)

    t = re.sub(r"₹\s*([\d,]+(?:\.\d+)?)(?:\s*rupees)?(?!\w)", _repl_rupees, t, flags=re.IGNORECASE)
    t = re.sub(r"(?:rs\.?|inr)\s*([\d,]+(?:\.\d+)?)(?:\s*rupees)?(?!\w)", _repl_rupees, t, flags=re.IGNORECASE)

    def _repl_num_rupees(m):
        raw = m.group(1).replace(",", "")
        try:
            n = round(float(raw))
            return f"{n:,} rupees"
        except Exception:
            return m.group(0)

    t = re.sub(r"(?<!\d)([\d,]+)\s*rupees\b", _repl_num_rupees, t, flags=re.IGNORECASE)
    # Standalone price numbers matching catalog values
    t = re.sub(r"\b(799|899|999|1199|1299|1499|1599|1699|3199|6999|7999)\b(?!\s*(?:millilitres|ml|percent|%|cm|mm|inches|gm|g|kg))", _repl_num_rupees, t, flags=re.IGNORECASE)

    t = re.sub(r"(\d+)\s*%", r"\1 percent", t)
    t = re.sub(r"\s*&\s*", " and ", t)
    t = re.sub(r"\s+[–—\-]\s+", ", ", t)
    t = re.sub(r"(\d+)\s*ml\b", r"\1 millilitres", t, flags=re.IGNORECASE)
    t = re.sub(r"[*#_`~|>{}\[\]]", " ", t)
    t = re.sub(r"\.{2,}", ".", t)
    t = re.sub(r",{2,}", ",", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def chunk_text(text: str) -> List[str]:
    if not text:
        return []
    normalized = normalize_for_speech(text)
    if not normalized:
        return []

    # Split ONLY at sentence ends (. ! ?) - never at commas
    raw_sentences = [
        s.strip() for s in re.split(r"(?<=[.?!])\s+(?=[A-Z0-9\"'])", normalized) if s.strip()
    ]

    chunks: List[str] = []
    buffer = ""

    for s in raw_sentences:
        if not buffer:
            buffer = s
        elif len(buffer) + len(s) + 1 <= 80:
            buffer += " " + s
        else:
            chunks.append(buffer)
            buffer = s

    if buffer:
        if chunks and len(buffer) < 25 and len(chunks[-1]) + len(buffer) + 1 <= 85:
            chunks[-1] += " " + buffer
        else:
            chunks.append(buffer)

    final_chunks: List[str] = []
    for c in chunks:
        if len(c) <= 80:
            final_chunks.append(c)
            continue

        # Split long sentences ONLY at word spaces, NEVER at commas
        remaining = c
        while len(remaining) > 80:
            space_idx = remaining[:80].rfind(" ")
            if space_idx >= 20:
                split_idx = space_idx
            else:
                split_idx = 80

            part = remaining[:split_idx].strip()
            if part:
                final_chunks.append(part)
            remaining = remaining[split_idx:].strip()

        if remaining:
            if final_chunks and len(remaining) < 25 and len(final_chunks[-1]) + len(remaining) + 1 <= 85:
                final_chunks[-1] += " " + remaining
            else:
                final_chunks.append(remaining)

    return [ch for ch in final_chunks if ch]


def build_product_speech(
    product: Dict[str, Any],
    customer_name: Optional[str] = None,
    variant_index: Optional[int] = None,
    has_pair_up: bool = False,
) -> Tuple[List[str], str]:
    """Return (chunks, full_text). Max 3 chunks, ~10-12s, no customer name, plain price."""
    if not product:
        fallback = "Here are our sustainable products. Tap any item to explore."
        return [fallback], fallback

    clean_name = clean_product_name(product.get("name"))
    if variant_index is not None:
        v_idx = abs(int(variant_index)) % 3
    else:
        v_idx = len(clean_name) % 3

    chunks: List[str] = []
    key_feature = extract_key_feature(product)

    # 1. Intro + key feature
    intro_templates = [
        f"This is the {clean_name}, {key_feature}.",
        f"Here is the {clean_name}, {key_feature}.",
        f"Meet the {clean_name}, {key_feature}.",
    ]
    chunks.append(normalize_for_speech(intro_templates[v_idx]))

    # 2. Price in plain sentence + real colours only
    raw_price = product.get("salePrice") or product.get("price") or 0
    try:
        if isinstance(raw_price, (int, float)):
            price_num = round(float(raw_price))
        else:
            clean_p = re.sub(r"[^\d.]", "", str(raw_price))
            price_num = round(float(clean_p)) if clean_p else 0
    except Exception:
        price_num = 0
    price_str = format_rupees(price_num) if price_num > 0 else ""

    colours = extract_product_colours(product)
    if price_str and len(colours) >= 1:
        colour_text = colours[0] if len(colours) == 1 else (
            f"{colours[0]} and {colours[1]}" if len(colours) == 2 else f"{', '.join(colours[:2])} and {colours[2]}"
        )
        chunks.append(normalize_for_speech(f"It's {price_str} and comes in {colour_text}."))
    elif price_str:
        chunks.append(normalize_for_speech(f"It's {price_str}."))
    elif len(colours) >= 1:
        colour_text = colours[0] if len(colours) == 1 else (
            f"{colours[0]} and {colours[1]}" if len(colours) == 2 else f"{', '.join(colours[:2])} and {colours[2]}"
        )
        chunks.append(normalize_for_speech(f"It comes in {colour_text}."))

    # 3. Optional short rotating CTA (skip if pair-up on screen)
    if not has_pair_up:
        closing_templates = [
            "Tap Add to Cart whenever you're ready.",
            "Have a look around, I'm here if you need me.",
            "Take your time to explore, or tap Add to Cart.",
        ]
        chunks.append(closing_templates[v_idx])

    return chunks, " ".join(chunks)


def answer_product_question(product: Dict[str, Any], question: str = "") -> str:
    """Answers follow-up questions from product data in 1 to 2 friendly sentences."""
    if not product:
        return "I'd be happy to help! Which product would you like to know about?"

    raw_price = product.get("salePrice") or product.get("price") or 0
    try:
        if isinstance(raw_price, (int, float)):
            price_num = round(float(raw_price))
        else:
            clean_p = re.sub(r"[^\d.]", "", str(raw_price))
            price_num = round(float(clean_p)) if clean_p else 0
    except Exception:
        price_num = 0
    price_str = format_rupees(price_num) if price_num > 0 else ""

    # 1. Colours
    if any(k in q for k in ("colour", "color", "shade", "variant")):
        colours = extract_product_colours(product)
        if len(colours) >= 2:
            return f"The {clean_name} is available in {' and '.join(colours)}. You can tap any color swatch on screen to switch!"
        if len(colours) == 1:
            return f"The {clean_name} comes in our signature {colours[0]} finish."
        return f"The {clean_name} comes in its natural single finish as shown on the screen."

    # 2. Material
    if any(k in q for k in ("material", "made of", "plastic", "eco", "safe")):
        if "stainless steel" in desc:
            return f"The {clean_name} is made from upcycled rice husk with a food-safe stainless steel interior. It is 100 percent BPA-free and non-toxic."
        if "bamboo" in desc:
            return f"The {clean_name} is crafted from natural bamboo fibres and crop biocomposite. It is durable, non-toxic, and climate positive."
        return f"The {clean_name} is crafted from sustainable rice husk biocomposite and zero virgin plastic. It is food-safe, non-toxic, and BPA-free."

    # 3. Kids / School
    if any(k in q for k in ("kid", "child", "school", "office")):
        if "school" in desc or "kids" in desc:
            return f"Yes, the {clean_name} is lightweight, durable, and perfect for school kids as well as office and daily travel."
        return "Yes! It is durable, break-resistant, and 100 percent food-safe, making it great for children and the whole family."

    # 4. Care instructions / microwave / dishwasher
    if any(k in q for k in ("wash", "clean", "microwave", "dishwasher")):
        if "microwave" in desc or "dishwasher" in desc:
            return f"Yes, the {clean_name} is microwave-reheat safe and completely dishwasher friendly."
        return "You can easily clean it by hand washing with mild dish soap and warm water."

    # 5. Price
    if any(k in q for k in ("price", "cost", "how much", "rate")):
        return f"The {clean_name} is {price_str}."

    return f"The {clean_name} is made from sustainable rice husk for {price_str}. Let me know if you would like to add it to your cart!"
