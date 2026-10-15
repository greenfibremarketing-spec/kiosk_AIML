"""Interactive terminal chat CLI — Greenie AI Kiosk Sales Flow Simulator.

Simulates the real kiosk interaction:
  - Avatar greets, auto-sends SPEECH_DONE (simulates TTS finishing on device)
  - Avatar asks name  → type your name  (or press Enter to skip)
  - Avatar shows categories → pick a NUMBER to simulate tapping a button
  - Avatar shows products   → pick a NUMBER to simulate tapping a product card
  - Free-text questions     → just type  (simulates speaking)

Commands: /reset | /trace | /chunks <query> | /exit
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import uuid
from app.brain import ask_avatar_turn
from app.callbacks import TraceCallbackHandler
from app.config import settings
from app.rag import print_retrieval_inspect
from app.sessions import session_manager


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _new_id() -> str:
    return f"cli-{uuid.uuid4().hex[:8]}"


def _turn(session_id, message="", command=None, trace_handler=None):
    return ask_avatar_turn(
        message=message,
        session_id=session_id,
        trace_handler=trace_handler,
        command=command,
    )


def _print_avatar(reply: str):
    print(f"\n  Avatar: {reply}\n")


def _auto_ack_speech(session_id, action):
    """Send SPEECH_DONE automatically (simulates frontend TTS-done event)."""
    if not action:
        return None
    speech_id = action.get("speech_id")
    if not speech_id:
        return None
    print(f"  [auto] SPEECH_DONE sent (TTS finished)…")
    result = _turn(session_id, command={"action": "SPEECH_DONE", "speech_id": speech_id, "turn_id": _new_id()})
    _print_avatar(result.reply)
    return result.action


def _handle_ask_name(session_id, action):
    """Prompt user to type their name → sends SET_NAME or SKIP_NAME."""
    prompt = action.get("prompt", "What should I call you?")
    skip_allowed = action.get("skip_allowed", True)
    print(f"  ❓ {prompt}")
    hint = " (or press Enter to skip)" if skip_allowed else ""
    name = input(f"  You (your name{hint}): ").strip()
    if name:
        result = _turn(session_id, command={"action": "SET_NAME", "name": name, "turn_id": _new_id()})
    else:
        result = _turn(session_id, command={"action": "SKIP_NAME", "turn_id": _new_id()})
    _print_avatar(result.reply)
    return result.action


def _handle_show_categories(session_id, action):
    """Display category buttons as numbered list → sends SELECT_CATEGORY."""
    categories = action.get("categories", [])
    show_all = action.get("show_all")

    if not categories:
        return None  # Categories not populated yet (happens on first greeting action)

    print("  ┌─ Categories (tap a button) ─────────────")
    for i, cat in enumerate(categories, 1):
        print(f"  │  {i}. {cat.get('name', cat.get('id', '?'))}")
    if show_all:
        print(f"  │  0. Show all products")
    print("  └─────────────────────────────────────────")

    while True:
        choice = input("  You (pick number): ").strip()
        if show_all and choice == "0":
            result = _turn(session_id, command={"action": "SHOW_ALL", "turn_id": _new_id()})
            break
        if choice.isdigit() and 1 <= int(choice) <= len(categories):
            cat = categories[int(choice) - 1]
            result = _turn(session_id, command={"action": "SELECT_CATEGORY", "category_id": cat["id"], "turn_id": _new_id()})
            break
        print(f"  ⚠ Enter a number 1–{len(categories)}.")

    _print_avatar(result.reply)
    return result.action


def _handle_show_products(session_id, action, trace_handler=None):
    """Display product cards as numbered list → sends SELECT_PRODUCT or free-text."""
    products = action.get("products", [])

    if not products:
        return None

    print("  ┌─ Products (tap a card or ask a question) ─")
    for i, p in enumerate(products, 1):
        name = p.get("name") or p.get("title") or p.get("sku", "?")
        price = p.get("price", "")
        price_str = f"  ₹{price}" if price else ""
        print(f"  │  {i}. {name}{price_str}")
    print(f"  │  0. ← Go back")
    print("  └────────────────────────────────────────────")

    choice = input("  You (pick number OR type a question): ").strip()

    if choice == "0":
        result = _turn(session_id, command={"action": "NAVIGATE_BACK", "turn_id": _new_id()})
    elif choice.isdigit() and 1 <= int(choice) <= len(products):
        p = products[int(choice) - 1]
        sku = p.get("sku") or p.get("id", "")
        result = _turn(session_id, command={"action": "SELECT_PRODUCT", "sku": sku, "turn_id": _new_id()})
    else:
        # Treat as spoken question
        result = _turn(session_id, message=choice, trace_handler=trace_handler)

    _print_avatar(result.reply)
    return result.action


def _dispatch(session_id, action, trace_handler=None):
    """Route incoming action to the right handler. Returns the next pending action."""
    if not action:
        return None
    kind = action.get("action", "")

    # Greeting with TTS → auto-acknowledge
    if action.get("awaiting_speech_done") or action.get("speech_id"):
        return _auto_ack_speech(session_id, action)

    if kind == "ASK_NAME":
        return _handle_ask_name(session_id, action)

    if kind == "SHOW_CATEGORIES":
        return _handle_show_categories(session_id, action)

    if kind in ("SHOW_PRODUCTS", "SHOW_PRODUCT_DETAIL"):
        return _handle_show_products(session_id, action, trace_handler)

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Greenie AI Kiosk — Sales Flow CLI")
    parser.add_argument("--trace", action="store_true", help="Enable per-turn trace output")
    args = parser.parse_args()

    show_trace = args.trace
    session_id = f"cli-{uuid.uuid4().hex[:6]}"
    trace_handler = TraceCallbackHandler() if show_trace else None

    print("\n" + "=" * 60)
    print("  GREENIE AI KIOSK — SALES FLOW CLI")
    print("=" * 60)
    print(f"  Session  : {session_id}")
    print(f"  Provider : {settings.llm_provider.upper()} ({settings.active_model_name})")
    print(f"  Tip      : Pick numbers = tapping buttons | Type text = speaking")
    print(f"  Commands : /reset  /trace  /chunks <q>  /exit")
    print("=" * 60 + "\n")

    # ── Kick off the conversation ─────────────────────────────────────────────
    print("  [Kiosk] Customer approaches the screen…")
    result = _turn(session_id, message="hi")
    _print_avatar(result.reply)

    pending = _dispatch(session_id, result.action, trace_handler)
    while pending:
        pending = _dispatch(session_id, pending, trace_handler)

    # ── Free conversation loop ────────────────────────────────────────────────
    while True:
        try:
            user_input = input("You: ").strip()
            if not user_input:
                continue

            if user_input.lower() in ("/exit", "exit", "quit", "q"):
                print("\nAvatar: Thank you for visiting Greenie! Have a wonderful day.")
                break

            if user_input.lower() == "/reset":
                session_manager.reset_session(session_id)
                print("[System] Memory cleared. Restarting flow…\n")
                result = _turn(session_id, message="hi")
                _print_avatar(result.reply)
                pending = _dispatch(session_id, result.action, trace_handler)
                while pending:
                    pending = _dispatch(session_id, pending, trace_handler)
                continue

            if user_input.lower() == "/trace":
                show_trace = not show_trace
                trace_handler = TraceCallbackHandler() if show_trace else None
                print(f"[System] Trace: {'ENABLED' if show_trace else 'DISABLED'}\n")
                continue

            if user_input.startswith("/chunks "):
                query = user_input[len("/chunks "):].strip()
                if query:
                    print_retrieval_inspect(query)
                continue

            # Free-text (spoken message)
            result = _turn(session_id, message=user_input, trace_handler=trace_handler)
            _print_avatar(result.reply)

            if show_trace and trace_handler:
                print(trace_handler.render_trace())

            pending = _dispatch(session_id, result.action, trace_handler)
            while pending:
                pending = _dispatch(session_id, pending, trace_handler)

        except (KeyboardInterrupt, EOFError):
            print("\n\nAvatar: Goodbye! Have a green day.")
            break
        except Exception as e:
            print(f"\n[Error] {str(e)}\n")


if __name__ == "__main__":
    main()
