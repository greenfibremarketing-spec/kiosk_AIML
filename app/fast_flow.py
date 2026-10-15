"""Fast 5-step kiosk conversation engine.

Step 1  WAKE      -> greeting + speech_id (input locked until TTS ends)
Step 2  NAME      -> ask name (skippable)
Step 3  CATEGORY  -> live category buttons
Step 4  PRODUCT   -> one product at a time; Next/Cheaper/More info/Compare/Take it
Step 5  BUY       -> confirm -> checkout URL -> goodbye -> auto-reset

Always-available: CALL_STAFF / HELP / RESTART

Silence detection (called by WS endpoint on a timer):
  8 s idle   -> push {"type":"ping_silence","data":"Still there?"}
  2 strikes  -> push {"type":"auto_reset","data":"Resetting..."} and reset ctx

All transitions are pure-Python. No LLM call for button actions (<100 ms).
"""

from __future__ import annotations
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from app.product_repository import is_active, product_view, valid_price
from app.tools import get_product_repository
from app.greenfibre_repository import ProductAPIError
from app.sales_state import SalesState
from app.recommendation import build_recommendation_action, handle_accept_recommendation, handle_decline_recommendation

logger = logging.getLogger("greenie.fast_flow")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _price_str(p: Dict[str, Any]) -> Optional[str]:
    v = p.get("price")
    try:
        if valid_price(v):
            return f"\u20b9{int(float(v)):,}"
    except Exception:
        pass
    return None


def _card(p: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "sku": p.get("sku"),
        "name": p.get("name"),
        "price": _price_str(p),
        "image": (p.get("images") or [""])[0],
        "product_url": p.get("product_url", ""),
    }


def _active_products(catalog: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [p for p in catalog.get("products", []) if p.get("sku") and is_active(p)]


def _load_catalog() -> Dict[str, Any]:
    from app.sales_engine import get_verified_catalog
    return get_verified_catalog()


def _categories(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Dict[str, int] = {}
    for p in products:
        cat = p.get("category")
        if cat:
            seen[cat] = seen.get(cat, 0) + 1
    return [
        {"id": re.sub(r"[^a-z0-9]+", "-", c.lower()).strip("-"), "name": c, "count": seen[c]}
        for c in sorted(seen.keys())
    ]


def _extract_name(text: str) -> Optional[str]:
    t = text.strip()
    for pat in [
        r"(?:my name is|i am|i'm|call me|myself)\s+([A-Za-z]+)",
        r"(?:naam hai|mera naam)\s+([A-Za-z]+)",
    ]:
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            return m.group(1).title()
    words = re.findall(r"[A-Za-z]+", t)
    stop = {"hi", "hello", "hey", "greeny", "skip", "no", "what", "show", "bottle", "ok", "yes"}
    if 1 <= len(words) <= 2 and words[0].lower() not in stop and len(words[0]) >= 2:
        return words[0].title()
    return None


# ---------------------------------------------------------------------------
# Button builders
# ---------------------------------------------------------------------------

def _wake_buttons() -> List[Dict[str, str]]:
    return [{"id": "start", "label": "Start", "action": "RESTART"}]


def _name_buttons() -> List[Dict[str, str]]:
    return [{"id": "skip_name", "label": "Skip", "action": "SKIP_NAME"}]


def _category_buttons(cats: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    btns = [{"id": c["id"], "label": c["name"], "action": "SELECT_CATEGORY:" + c["id"]} for c in cats]
    btns.append({"id": "all", "label": "Show Everything", "action": "SHOW_ALL"})
    return btns


def _product_buttons(sku: str) -> List[Dict[str, str]]:
    return [
        {"id": "next",      "label": "Next",         "action": "NEXT_PRODUCT"},
        {"id": "cheaper",   "label": "Cheaper",      "action": "CHEAPER"},
        {"id": "more_info", "label": "More info",    "action": "SELECT_PRODUCT:" + sku},
        {"id": "compare",   "label": "Compare",      "action": "COMPARE"},
        {"id": "buy",       "label": "I'll take it", "action": "BUY:" + sku},
    ]


def _buy_buttons(name: Optional[str]) -> List[Dict[str, str]]:
    thanks_label = ("Thanks, " + name + "!") if name else "Thank you!"
    return [
        {"id": "checkout", "label": "Go to checkout", "action": "CHECKOUT"},
        {"id": "more",     "label": "Keep shopping",  "action": "SHOW_ALL"},
        {"id": "bye",      "label": thanks_label,     "action": "GOODBYE"},
    ]


def _always_available() -> List[Dict[str, str]]:
    return [
        {"id": "call_staff", "label": "Call staff", "action": "CALL_STAFF"},
        {"id": "help",       "label": "Help",       "action": "HELP"},
        {"id": "restart",    "label": "Start over", "action": "RESTART"},
    ]


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------

class FastFlowContext:
    __slots__ = (
        "stage", "name", "speech_id",
        "products", "cat_products", "product_index",
        "selected_sku", "silence_strikes",
    )

    def __init__(self) -> None:
        self.stage: str = "WAKE"
        self.name: Optional[str] = None
        self.speech_id: Optional[str] = None
        self.products: List[Dict[str, Any]] = []
        self.cat_products: List[Dict[str, Any]] = []
        self.product_index: int = 0
        self.selected_sku: Optional[str] = None
        self.silence_strikes: int = 0

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]


_ff: Dict[str, FastFlowContext] = {}


def get_ff(session_id: str) -> FastFlowContext:
    if session_id not in _ff:
        _ff[session_id] = FastFlowContext()
    return _ff[session_id]


def drop_ff(session_id: str) -> None:
    _ff.pop(session_id, None)


# ---------------------------------------------------------------------------
# Silence detection
# ---------------------------------------------------------------------------

def silence_tick(session_id: str) -> Dict[str, Any]:
    """Call every 8 s of silence. Returns a push frame for the WS endpoint."""
    ctx = get_ff(session_id)
    ctx.silence_strikes += 1
    if ctx.silence_strikes == 1:
        return {"type": "ping_silence", "data": "Still there? Say something or tap a button."}
    ctx.reset()
    return {"type": "auto_reset", "data": "Resetting for the next person..."}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fast_transition(
    session_id: str,
    raw_message: str,
    current_sales_state: Optional[SalesState] = None,
    command: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Dict[str, Any], Optional[SalesState]]:
    """Return (reply, screen_action, state). Raises ProductAPIError on failure."""
    ctx = get_ff(session_id)
    ctx.silence_strikes = 0
    msg = raw_message.strip()
    ml = msg.lower()

    # Always-available controls
    if ml in ("call_staff", "call staff") or ml.startswith("call staff"):
        return (
            "Of course! I've called a store associate for you. They'll be here shortly.",
            {"action": "REQUEST_HUMAN", "buttons": _always_available(),
             "always": _always_available(), "input_enabled": False},
            current_sales_state,
        )

    if ml == "help":
        hint = _stage_hints(ctx)
        return (
            "You can say: " + hint,
            {"action": "SHOW_HINT", "hint": hint, "buttons": _stage_buttons(ctx),
             "always": _always_available(), "input_enabled": True},
            current_sales_state,
        )

    if ml in ("restart", "start over", "reset", "shuru se"):
        ctx.reset()
        return _step_wake(ctx, session_id)

    # Explicit command dispatch (overrides current stage if structured command provided)
    if command:
        cmd_action = command.get("action")
        if cmd_action == "SET_NAME":
            name_val = command.get("name") or _extract_name(msg) or msg
            return _step_collect_name(ctx, session_id, name_val)
        if cmd_action == "SKIP_NAME":
            return _step_collect_name(ctx, session_id, "skip")
        if cmd_action == "SELECT_CATEGORY":
            target = command.get("category_id") or command.get("category_name") or msg
            return _step_select_category(ctx, session_id, f"select_category:{target}", f"select_category:{target}".lower())
        if cmd_action == "SHOW_ALL":
            return _step_select_category(ctx, session_id, "show all", "show all")
        if cmd_action == "SPEECH_DONE":
            if ctx.stage in ("WAKE", "GREETING"):
                return _step_ask_name(ctx, session_id)
        if cmd_action == "REQUEST_RECOMMENDATION":
            sku = ctx.selected_sku or (ctx.cat_products[ctx.product_index % len(ctx.cat_products)].get("sku") if ctx.cat_products else None) or ""
            reco = build_recommendation_action(sku, ctx.name)
            if reco:
                return reco.get("message", ""), {**reco}, current_sales_state
            return "Sorry, suggestions are not available right now.", _wrap(ctx, "SHOW_HINT", {"hint": ""}), current_sales_state
        if cmd_action == "ACCEPT_RECOMMENDATION":
            base_sku = ctx.selected_sku or ""
            result = handle_accept_recommendation(base_sku, "", ctx.name)
            return result.get("message", ""), result, current_sales_state
        if cmd_action == "DECLINE_RECOMMENDATION":
            result = handle_decline_recommendation(ctx.name)
            return result.get("message", ""), result, current_sales_state

    # Stage dispatch
    if ctx.stage == "WAKE":
        return _step_wake(ctx, session_id)
    if ctx.stage == "GREETING":
        if ml in ("speech_done", "greeting_done") or msg == ctx.speech_id:
            return _step_ask_name(ctx, session_id)
        name_cand = _extract_name(msg)
        if name_cand or any(s in ml for s in ("skip", "next", "no", "nahi")):
            return _step_collect_name(ctx, session_id, msg)
        return (
            "Welcome! Please wait just a moment.",
            _wrap(ctx, "AWAIT_SPEECH", {}, input_enabled=False),
            current_sales_state,
        )
    if ctx.stage == "NAME":
        return _step_collect_name(ctx, session_id, msg)
    if ctx.stage == "CATEGORY":
        return _step_select_category(ctx, session_id, msg, ml)
    if ctx.stage == "PRODUCT":
        return _step_product_action(ctx, session_id, msg, ml)
    if ctx.stage == "BUY":
        return _step_buy_action(ctx, session_id, msg, ml)

    ctx.reset()
    return _step_wake(ctx, session_id)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _step_wake(ctx: FastFlowContext, session_id: str) -> Tuple[str, Dict[str, Any], None]:
    ctx.reset()
    ctx.stage = "GREETING"
    ctx.speech_id = uuid.uuid4().hex
    reply = "Hi! I'm Greeny, your shopping guide at Green Fibre. I'm here to help you discover our eco-friendly products, made from rice husk."
    return reply, {
        "action": "SHOW_GREETING",
        "speech_id": ctx.speech_id,
        "awaiting_speech_done": True,
        "message": reply,
        "buttons": [],
        "always": _always_available(),
        "input_enabled": False,
    }, None


def _step_ask_name(ctx: FastFlowContext, session_id: str) -> Tuple[str, Dict[str, Any], None]:
    ctx.stage = "NAME"
    reply = "Before we start, may I know your name?"
    return reply, _wrap(ctx, "ASK_NAME", {"prompt": reply, "message": reply, "skip_allowed": True},
                        buttons=_name_buttons(), input_enabled=True), None


def _step_collect_name(ctx: FastFlowContext, session_id: str, msg: str) -> Tuple[str, Dict[str, Any], None]:
    lower = msg.lower()
    skip_words = {"skip", "skip name", "next", "no", "nahi", "chodo", "aage", "don't want", "dont want"}
    if any(s in lower for s in skip_words):
        ctx.name = None
    else:
        ctx.name = _extract_name(msg) or (msg.title() if len(msg.split()) <= 2 and all(w.isalpha() for w in msg.split()) else None)

    try:
        catalog = _load_catalog()
        ctx.products = _active_products(catalog)
    except Exception:
        raise ProductAPIError("Product information is temporarily unavailable.")

    cats = _categories(ctx.products)
    ctx.stage = "CATEGORY"
    if ctx.name:
        reply = f"Nice to meet you, {ctx.name}! Have a look at our collection. Tap a category, or ask me anything."
    else:
        reply = "No problem! Take a look around. Tap any category, or just ask me anything."
    return reply, _wrap(ctx, "SHOW_CATEGORIES", {"categories": cats, "show_all": True, "message": reply},
                        buttons=_category_buttons(cats), input_enabled=True), None


def _find_in_products(products: List[Dict[str, Any]], target: str) -> Optional[Dict[str, Any]]:
    if not target:
        return None
    tl = target.strip().lower()
    for p in products:
        if (p.get("sku") or "").lower() == tl or (p.get("id") or "").lower() == tl or (p.get("name") or "").lower() == tl:
            return p
        for v in p.get("variants", []):
            if (v.get("sku") or "").lower() == tl or (v.get("id") or "").lower() == tl or (v.get("name") or "").lower() == tl:
                return {**p, "sku": v.get("sku"), "variant_id": v.get("id")}
    for p in products:
        pn = (p.get("name") or "").lower()
        if len(tl) >= 3 and (tl in pn or pn in tl):
            return p
    return None


def _show_specific_product(ctx: FastFlowContext, prod: Dict[str, Any]) -> Tuple[str, Dict[str, Any], None]:
    view = product_view(prod)
    p_name = view.get("name", "this product")
    price = _price_str(view) or "price on request"
    desc = (prod.get("description") or prod.get("shortDescription") or "")[:120].rstrip()
    sku = view.get("sku") or prod.get("sku") or ""
    pitch = f" {desc}." if desc else ""
    great_choice = f"Great choice, {ctx.name}! " if ctx.name else "Great choice! "
    reply = f"{great_choice}Here is our {p_name} for {price}.{pitch} Would you like to see its colours, price or other details?"
    ctx.stage = "PRODUCT"
    ctx.selected_sku = sku
    return (
        reply,
        _wrap(ctx, "SHOW_PRODUCT",
              {"product": _card({**prod, **view}), "description": desc, "sku": sku},
              buttons=_product_buttons(sku)),
        None,
    )


def _step_select_category(
    ctx: FastFlowContext, session_id: str, msg: str, ml: str,
) -> Tuple[str, Dict[str, Any], None]:
    if ml.startswith("select_product:"):
        target = msg.split(":", 1)[1].strip()
        matched = _find_in_products(ctx.products, target)
        if matched:
            ctx.cat_products = [matched]
            ctx.product_index = 0
            ctx.stage = "PRODUCT"
            ctx.selected_sku = matched.get("sku")
            return _show_specific_product(ctx, matched)

    cat_id: Optional[str] = None
    if ml.startswith("select_category:"):
        cat_id = ml.split(":", 1)[1].strip()
    elif ml in ("show_all", "show all", "all", "sab dikhao", "pura collection"):
        cat_id = "all"
    else:
        for c in _categories(ctx.products):
            if c["id"] in ml or c["name"].lower() in ml:
                cat_id = c["id"]
                break

    if cat_id is None or cat_id == "all":
        ctx.cat_products = list(ctx.products)
        cat_label = "our full collection"
    else:
        ctx.cat_products = [
            p for p in ctx.products
            if re.sub(r"[^a-z0-9]+", "-", (p.get("category") or "").lower()).strip("-") == cat_id
            or (p.get("category") or "").lower() == cat_id
            or cat_id in (p.get("name") or "").lower()
        ] or list(ctx.products)
        cat_label = cat_id.replace("-", " ").title()
        for c in _categories(ctx.products):
            if c["id"] == cat_id or c["name"].lower() == cat_id.lower():
                cat_label = c["name"]
                break

    if not ctx.cat_products:
        cats = _categories(ctx.products)
        return (
            "I couldn't find products there. Want to see everything?",
            _wrap(ctx, "SHOW_CATEGORIES", {"categories": cats}, buttons=_category_buttons(cats)),
            None,
        )

    count = len(ctx.cat_products)
    names = [p.get("name") for p in ctx.cat_products if p.get("name")]
    if count == 0:
        reply = f"I couldn't find any products in {cat_label} right now. Want to see everything?"
    elif count == 1:
        reply = f"In {cat_label}, we have {names[0]}. Would you like to check it out?"
    elif count == 2:
        reply = f"In {cat_label}, we have 2 products: {names[0]} and {names[1]}. Which one would you like to see?"
    elif count <= 4:
        names_str = ", ".join(names[:-1]) + " and " + names[-1]
        reply = f"In {cat_label}, we have {count} products: {names_str}. Which one would you like to explore?"
    else:
        names_str = ", ".join(names[:3])
        reply = f"In {cat_label}, we have {count} products including {names_str}. Which one would you like to explore?"

    if ctx.name:
        reply = f"{ctx.name}, {reply[0].lower() + reply[1:]}"

    ctx.product_index = 0
    ctx.stage = "CATEGORY"
    return (
        reply,
        _wrap(ctx, "SHOW_PRODUCTS", {
            "products": [_card(p) for p in ctx.cat_products],
            "count": count,
            "category": cat_label,
            "message": reply,
        }, buttons=[{"id": p["sku"], "label": p["name"], "action": "SELECT_PRODUCT:" + p["sku"]} for p in ctx.cat_products[:4]]),
        None,
    )


def _step_product_action(
    ctx: FastFlowContext, session_id: str, msg: str, ml: str,
) -> Tuple[str, Dict[str, Any], None]:

    if ml in ("next", "next_product", "another", "next one", "show next", "agla", "aur dikhao"):
        ctx.product_index = (ctx.product_index + 1) % max(len(ctx.cat_products), 1)
        return _show_current_product(ctx)

    if ml in ("cheaper", "something cheaper", "less expensive", "sasta", "budget", "cheaper option"):
        cur: Optional[Dict[str, Any]] = ctx.cat_products[ctx.product_index] if ctx.cat_products else None
        cur_price = 0.0
        try:
            if cur and valid_price(cur.get("price")):
                cur_price = float(cur["price"])
        except Exception:
            pass
        cheaper = [
            p for p in ctx.cat_products
            if valid_price(p.get("price")) and float(p["price"]) < cur_price * 0.95
        ]
        if cheaper:
            ctx.cat_products = cheaper
            ctx.product_index = 0
            return _show_current_product(ctx)
        sku0 = ctx.cat_products[0]["sku"] if ctx.cat_products else ""
        return (
            "These are already our most affordable options. Want to compare?",
            _wrap(ctx, "SHOW_PRODUCTS", {"products": [_card(p) for p in ctx.cat_products]},
                  buttons=_product_buttons(sku0)),
            None,
        )

    if ml in ("compare", "compare products", "compare items"):
        shown = ctx.cat_products[:3]
        return (
            "Here are some options to compare.",
            _wrap(ctx, "SHOW_PRODUCTS", {"products": [_card(p) for p in shown], "compare_mode": True},
                  buttons=_category_buttons(_categories(ctx.products)) + [
                      {"id": "back", "label": "Back", "action": "SHOW_ALL"}
                  ]),
            None,
        )

    buy_phrases = ("buy", "i'll take it", "ill take it", "add to cart", "purchase", "buy it", "le lo")
    if ml.startswith("buy:") or ml in buy_phrases:
        sku = msg.split(":", 1)[1].strip() if ml.startswith("buy:") else (
            ctx.cat_products[ctx.product_index].get("sku") if ctx.cat_products else None
        )
        ctx.selected_sku = sku
        ctx.stage = "BUY"
        return _step_confirm_buy(ctx, sku)

    more_phrases = ("more info", "more information", "details", "tell me more", "aur batao")
    if ml.startswith("select_product:") or ml in more_phrases:
        target = msg.split(":", 1)[1].strip() if ml.startswith("select_product:") else (
            ctx.cat_products[ctx.product_index].get("sku") if ctx.cat_products else ""
        )
        matched = _find_in_products(ctx.products, target)
        if matched:
            ctx.selected_sku = matched.get("sku")
            return _show_specific_product(ctx, matched)
        try:
            repo = get_product_repository()
            prod = repo.get(target) if target else None
            if prod:
                return _show_specific_product(ctx, prod)
        except Exception:
            pass
        return _show_current_product(ctx)

    # Fallback: treat as category re-selection
    return _step_select_category(ctx, session_id, msg, ml)


def _show_current_product(ctx: FastFlowContext) -> Tuple[str, Dict[str, Any], None]:
    if not ctx.cat_products:
        return (
            "I couldn't find any products right now. Want to start over?",
            _wrap(ctx, "NO_PRODUCTS", {}, buttons=_always_available()),
            None,
        )
    p = ctx.cat_products[ctx.product_index % len(ctx.cat_products)]
    name = p.get("name", "this product")
    price = _price_str(p) or "price on request"
    desc = (p.get("description") or "")[:80].rstrip()
    count = len(ctx.cat_products)
    idx = ctx.product_index + 1
    reply = "Try the " + name + ". " + desc + ". It's " + price + "."
    if count > 1:
        reply += " (" + str(idx) + " of " + str(count) + ")"
    return (
        reply,
        _wrap(ctx, "SHOW_PRODUCT_CARD",
              {"product": _card(p), "index": idx, "total": count},
              buttons=_product_buttons(p.get("sku", ""))),
        None,
    )


def _step_confirm_buy(ctx: FastFlowContext, sku: Optional[str]) -> Tuple[str, Dict[str, Any], None]:
    name_part = (", " + ctx.name) if ctx.name else ""
    reply = "Add it to your cart" + name_part + "? Tap 'Go to checkout' or keep shopping."
    return reply, _wrap(ctx, "CONFIRM_BUY", {"sku": sku}, buttons=_buy_buttons(ctx.name)), None


def _step_buy_action(
    ctx: FastFlowContext, session_id: str, msg: str, ml: str,
) -> Tuple[str, Dict[str, Any], None]:
    if ml in ("checkout", "go to checkout", "confirm", "yes", "haan"):
        url = ""
        if ctx.selected_sku:
            for p in ctx.cat_products:
                if p.get("sku") == ctx.selected_sku:
                    url = p.get("product_url", "")
                    break
            if not url:
                try:
                    repo = get_product_repository()
                    prod = repo.get(ctx.selected_sku)
                    if prod:
                        url = prod.get("product_url", "")
                except Exception:
                    pass
        name_part = (", " + ctx.name) if ctx.name else ""
        return (
            "Done! Anything else" + name_part + ", or go to checkout?",
            _wrap(ctx, "CHECKOUT_HANDOFF", {"sku": ctx.selected_sku, "product_url": url},
                  buttons=[
                      {"id": "more", "label": "Keep shopping", "action": "SHOW_ALL"},
                      {"id": "done", "label": "All done",      "action": "GOODBYE"},
                  ]),
            None,
        )

    if ml in ("goodbye", "done", "all done", "bye", "thanks", "thank you", "aur nahi"):
        thanks = ("Thanks, " + ctx.name + "! Have a great day.") if ctx.name else "Thank you! Have a great day."
        ctx.reset()
        return (
            thanks,
            {"action": "GOODBYE", "message": thanks, "auto_reset_after_ms": 4000,
             "buttons": [], "always": _always_available(), "input_enabled": False},
            None,
        )

    if ml in ("show_all", "show all", "more", "keep shopping"):
        ctx.stage = "PRODUCT"
        ctx.product_index = 0
        return _show_current_product(ctx)

    return _step_confirm_buy(ctx, ctx.selected_sku)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _wrap(
    ctx: FastFlowContext,
    action_name: str,
    payload: Dict[str, Any],
    *,
    buttons: Optional[List[Dict[str, str]]] = None,
    input_enabled: bool = True,
) -> Dict[str, Any]:
    return {
        "action": action_name,
        "buttons": buttons if buttons is not None else _stage_buttons(ctx),
        "always": _always_available(),
        "input_enabled": input_enabled,
        **payload,
    }


def _stage_buttons(ctx: FastFlowContext) -> List[Dict[str, str]]:
    if ctx.stage == "WAKE":
        return _wake_buttons()
    if ctx.stage in ("GREETING", "NAME"):
        return _name_buttons()
    if ctx.stage == "CATEGORY":
        return _category_buttons(_categories(ctx.products))
    if ctx.stage == "PRODUCT" and ctx.cat_products:
        sku = ctx.cat_products[ctx.product_index % len(ctx.cat_products)].get("sku", "")
        return _product_buttons(sku)
    if ctx.stage == "BUY":
        return _buy_buttons(ctx.name)
    return _always_available()


def _stage_hints(ctx: FastFlowContext) -> str:
    return {
        "WAKE":     "Say Hello or tap Start.",
        "GREETING": "Wait for the greeting to finish.",
        "NAME":     "Say your name, or tap Skip.",
        "CATEGORY": "Say a category name or tap a button.",
        "PRODUCT":  "Say Next, Cheaper, More info, Compare, or I'll take it.",
        "BUY":      "Say Yes to confirm, or Keep shopping.",
    }.get(ctx.stage, "Say what you need or tap a button.")
