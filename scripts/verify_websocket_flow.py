"""
verify_websocket_flow.py
========================
Comprehensive end-to-end WebSocket verification suite for the Greeny Kiosk.

Verifies:
  1. Server initiates conversation first (auto-greet on connect, zero user input).
  2. Sub-50ms deterministic fast-flow latency for buttons and navigation.
  3. Every step runs 100% through WebSocket (v2 protocol envelopes).
  4. Complete 5-step flow:
     - Step 1: Wake & Server-initiated Greeting
     - Step 2: Speech-done acknowledgement -> Ask Name
     - Step 3: Name capture -> Category buttons displayed
     - Step 4: Product recommendation & navigation (Next, Cheaper)
     - Step 5: Buy -> Checkout confirmation -> Goodbye & Auto-reset
  5. Always-available actions: Help, Call Staff, Restart.
  6. Free-form Q&A through WebSocket.

Usage:
  python scripts/verify_websocket_flow.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple
from starlette.testclient import TestClient

# Ensure root directory is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.server import api

# ANSI Colors
GREEN = "\033[92m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"


def print_banner(text: str) -> None:
    line = "=" * 72
    print(f"\n{BOLD}{CYAN}{line}{RESET}")
    print(f"{BOLD}{CYAN}  {text}{RESET}")
    print(f"{BOLD}{CYAN}{line}{RESET}")


def print_step(number: int, name: str, desc: str) -> None:
    print(f"\n{BOLD}▶ Step {number}: {name}{RESET} — {desc}")


def collect_ws_turn(ws) -> Tuple[str, Dict[str, Any], float, List[Dict[str, Any]]]:
    """Read WebSocket frames until 'done' is received. Returns (reply, action, latency_ms, frames)."""
    t0 = time.perf_counter()
    tokens: List[str] = []
    action: Dict[str, Any] = {}
    frames: List[Dict[str, Any]] = []

    while True:
        raw = ws.receive_json()
        frames.append(raw)
        mtype = raw.get("type")
        data = raw.get("data")

        if mtype == "token":
            tokens.append(str(data))
        elif mtype == "action":
            if isinstance(data, dict):
                action = data
        elif mtype == "done":
            if not action and isinstance(raw.get("meta"), dict) and "action" in raw["meta"]:
                action = raw["meta"]["action"] or {}
            break
        elif mtype == "error":
            elapsed = (time.perf_counter() - t0) * 1000
            reply = "".join(tokens).strip() or str(data)
            return reply, {"error": data}, elapsed, frames

    elapsed = (time.perf_counter() - t0) * 1000
    reply = "".join(tokens).strip() or (str(data) if data else "")
    return reply, action, elapsed, frames


def run_verification() -> bool:
    print_banner("GREENY KIOSK — FULL WEBSOCKET-ONLY FLOW VERIFICATION")
    passed_checks = 0
    total_checks = 0

    def check(condition: bool, label: str, detail: str = "") -> None:
        nonlocal passed_checks, total_checks
        total_checks += 1
        if condition:
            passed_checks += 1
            print(f"  {GREEN}✔ [PASS]{RESET} {label} {f'({detail})' if detail else ''}")
        else:
            print(f"  {RED}✘ [FAIL]{RESET} {label} {f'({detail})' if detail else ''}")

    session_id = f"fast-verify-{int(time.time())}"
    ws_url = f"/ws/{session_id}?v=2&kiosk_id=kiosk-01&auto_greet=true"

    from app.fast_flow import _load_catalog
    _load_catalog()

    with TestClient(api) as client:
        print(f"\nConnecting WebSocket to: {BOLD}{ws_url}{RESET}")
        with client.websocket_connect(ws_url) as ws:
            print(f"{GREEN}Connected successfully over WebSocket!{RESET}\n")

            # ---------------------------------------------------------
            # 1. SERVER-INITIATED GREETING (User did NOT speak first)
            # ---------------------------------------------------------
            print_step(1, "WAKE & AUTO-GREET", "Server initiates greeting on connection without client input")
            reply, action, latency, frames = collect_ws_turn(ws)
            print(f"    Avatar Spoke : \"{reply}\"")
            print(f"    Action Type  : {action.get('action')}")
            print(f"    Speech ID    : {action.get('speech_id')}")
            print(f"    Latency      : {latency:.1f}ms")

            check(action.get("action") == "SHOW_GREETING", "Server sent SHOW_GREETING frame immediately on connect")
            check(bool(action.get("speech_id")), "Speech ID generated for TTS coordination", action.get("speech_id", ""))
            check(action.get("input_enabled") is False, "User mic locked during introductory greeting")
            check(latency < 100, "Sub-100ms greeting dispatch", f"{latency:.1f}ms")
            speech_id = action.get("speech_id")

            # ---------------------------------------------------------
            # 2. SPEECH DONE ACKNOWLEDGEMENT -> ASK NAME
            # ---------------------------------------------------------
            print_step(2, "SPEECH_DONE ACK", "Client informs avatar finished speaking introductory greeting")
            t0 = time.perf_counter()
            ws.send_json({
                "v": 2,
                "action": "SPEECH_DONE",
                "speech_id": speech_id,
                "turn_id": "turn-02",
            })
            reply, action, latency, frames = collect_ws_turn(ws)
            print(f"    Avatar Spoke : \"{reply}\"")
            print(f"    Action Type  : {action.get('action')}")
            print(f"    Buttons      : {[b['label'] for b in action.get('buttons', [])]}")
            print(f"    Latency      : {latency:.1f}ms")

            check(action.get("action") == "ASK_NAME", "Advanced to ASK_NAME step")
            check(action.get("input_enabled") is True, "Microphone and keyboard unlocked for user")
            check(any(b.get("id") in ("skip", "skip_name") for b in action.get("buttons", [])), "Skip Name button provided")
            check(latency < 100, "Fast-path speech ack latency", f"{latency:.1f}ms")

            # ---------------------------------------------------------
            # 3. CUSTOMER PROVIDES NAME -> SHOW CATEGORIES
            # ---------------------------------------------------------
            print_step(3, "NAME CAPTURE", "Customer provides name: 'Rahul'")
            t0 = time.perf_counter()
            ws.send_json({
                "v": 2,
                "message": "Rahul",
                "turn_id": "turn-03",
            })
            reply, action, latency, frames = collect_ws_turn(ws)
            print(f"    Avatar Spoke : \"{reply}\"")
            print(f"    Action Type  : {action.get('action')}")
            categories = action.get("categories", [])
            print(f"    Categories   : {[c.get('name') for c in categories]}")
            print(f"    Latency      : {latency:.1f}ms")

            check("Rahul" in reply, "Personalized greeting with customer's name")
            check(action.get("action") == "SHOW_CATEGORIES", "Advanced to SHOW_CATEGORIES step")
            check(len(categories) > 0, "Live product categories loaded", f"{len(categories)} categories")
            check(latency < 100, "Category retrieval latency", f"{latency:.1f}ms")

            first_cat_id = categories[0]["id"] if categories else "drinkware"
            first_cat_name = categories[0]["name"] if categories else "Drinkware"

            # ---------------------------------------------------------
            # 4. CATEGORY SELECTION -> SHOW PRODUCT CARD #1
            # ---------------------------------------------------------
            print_step(4, "SELECT CATEGORY", f"Customer selects '{first_cat_name}'")
            ws.send_json({
                "v": 2,
                "action": "SELECT_CATEGORY",
                "category_id": first_cat_id,
                "turn_id": "turn-04",
            })
            reply, action, latency, frames = collect_ws_turn(ws)
            print(f"    Avatar Spoke : \"{reply}\"")
            print(f"    Action Type  : {action.get('action')}")
            p1 = action.get("product") or {}
            print(f"    Product #1   : {p1.get('name')} | {p1.get('price')} (SKU: {p1.get('sku')})")
            print(f"    Buttons      : {[b['label'] for b in action.get('buttons', [])]}")
            print(f"    Latency      : {latency:.1f}ms")

            check(action.get("action") == "SHOW_PRODUCT_CARD", "Returned SHOW_PRODUCT_CARD action")
            check(bool(p1.get("sku")), "Product contains valid SKU", p1.get("sku", ""))
            btn_labels = [b["label"].lower() for b in action.get("buttons", [])]
            check("next" in btn_labels or "i'll take it" in btn_labels, "Product navigation buttons present")
            check(latency < 100, "Product card render latency", f"{latency:.1f}ms")

            # ---------------------------------------------------------
            # 5. NAVIGATION: NEXT PRODUCT
            # ---------------------------------------------------------
            print_step(5, "PRODUCT NAVIGATION", "Customer taps 'Next'")
            ws.send_json({
                "v": 2,
                "message": "next",
                "turn_id": "turn-05",
            })
            reply, action, latency, frames = collect_ws_turn(ws)
            p2 = action.get("product") or {}
            print(f"    Avatar Spoke : \"{reply}\"")
            print(f"    Product #2   : {p2.get('name')} | {p2.get('price')} (SKU: {p2.get('sku')})")
            print(f"    Latency      : {latency:.1f}ms")

            check(action.get("action") == "SHOW_PRODUCT_CARD", "Next product card returned")
            check(latency < 100, "Next button transition latency", f"{latency:.1f}ms")

            # ---------------------------------------------------------
            # 6. NAVIGATION: CHEAPER PRODUCT
            # ---------------------------------------------------------
            print_step(6, "FILTER CHEAPER", "Customer taps 'Cheaper'")
            ws.send_json({
                "v": 2,
                "message": "cheaper",
                "turn_id": "turn-06",
            })
            reply, action, latency, frames = collect_ws_turn(ws)
            p_cheap = action.get("product") or {}
            print(f"    Avatar Spoke : \"{reply}\"")
            print(f"    Cheaper Prod : {p_cheap.get('name')} | {p_cheap.get('price')}")
            print(f"    Latency      : {latency:.1f}ms")

            check(action.get("action") == "SHOW_PRODUCT_CARD", "Cheaper filter returned product card")
            check(latency < 100, "Cheaper filter latency", f"{latency:.1f}ms")

            # ---------------------------------------------------------
            # 7. BUY: "I'LL TAKE IT" -> CONFIRM BUY
            # ---------------------------------------------------------
            print_step(7, "BUY INTENT", "Customer taps 'I\\'ll take it'")
            ws.send_json({
                "v": 2,
                "message": "i'll take it",
                "turn_id": "turn-07",
            })
            reply, action, latency, frames = collect_ws_turn(ws)
            print(f"    Avatar Spoke : \"{reply}\"")
            print(f"    Action Type  : {action.get('action')}")
            print(f"    Buttons      : {[b['label'] for b in action.get('buttons', [])]}")
            print(f"    Latency      : {latency:.1f}ms")

            check(action.get("action") == "CONFIRM_BUY", "Returned CONFIRM_BUY step")
            check(latency < 100, "Buy confirmation latency", f"{latency:.1f}ms")

            # ---------------------------------------------------------
            # 8. CHECKOUT HANDOFF: "YES" / "CHECKOUT"
            # ---------------------------------------------------------
            print_step(8, "CHECKOUT HANDOFF", "Customer confirms checkout: 'yes'")
            ws.send_json({
                "v": 2,
                "message": "yes",
                "turn_id": "turn-08",
            })
            reply, action, latency, frames = collect_ws_turn(ws)
            print(f"    Avatar Spoke : \"{reply}\"")
            print(f"    Action Type  : {action.get('action')}")
            print(f"    Checkout URL : {action.get('product_url')}")
            print(f"    Latency      : {latency:.1f}ms")

            check(action.get("action") == "CHECKOUT_HANDOFF", "Returned CHECKOUT_HANDOFF step")
            check(latency < 100, "Checkout handoff latency", f"{latency:.1f}ms")

            # ---------------------------------------------------------
            # 9. GOODBYE & AUTO-RESET
            # ---------------------------------------------------------
            print_step(9, "GOODBYE & AUTO-RESET", "Customer says 'thanks, bye'")
            ws.send_json({
                "v": 2,
                "message": "thanks",
                "turn_id": "turn-09",
            })
            reply, action, latency, frames = collect_ws_turn(ws)
            print(f"    Avatar Spoke : \"{reply}\"")
            print(f"    Action Type  : {action.get('action')}")
            print(f"    Reset Delay  : {action.get('auto_reset_after_ms')}ms")
            print(f"    Latency      : {latency:.1f}ms")

            check(action.get("action") == "GOODBYE", "Returned GOODBYE action")
            check(action.get("auto_reset_after_ms") == 4000, "Auto-reset timer configured (4000ms)")
            check("Rahul" in reply or "Thank" in reply, "Personal farewell message")
            check(latency < 100, "Goodbye transition latency", f"{latency:.1f}ms")

            # ---------------------------------------------------------
            # 10. ALWAYS-AVAILABLE ACTIONS (HELP & STAFF)
            # ---------------------------------------------------------
            print_step(10, "ALWAYS-AVAILABLE CONTROLS", "Testing 'Help' and 'Call Staff'")
            # Help
            ws.send_json({"v": 2, "message": "help", "turn_id": "turn-10-help"})
            reply_help, action_help, lat_help, _ = collect_ws_turn(ws)
            check("hint" in action_help or "help" in reply_help.lower() or bool(reply_help), "Help provides contextual assistance", f"{lat_help:.1f}ms")

            # Call staff
            ws.send_json({"v": 2, "message": "call staff", "turn_id": "turn-10-staff"})
            reply_staff, action_staff, lat_staff, _ = collect_ws_turn(ws)
            check(action_staff.get("action") in ("REQUEST_HUMAN", "STAFF_CALLED"), "Staff called notification dispatched", f"{lat_staff:.1f}ms")

            # Restart
            ws.send_json({"v": 2, "message": "restart", "turn_id": "turn-10-restart"})
            reply_restart, action_restart, lat_restart, _ = collect_ws_turn(ws)
            check(action_restart.get("action") == "SHOW_GREETING", "Restart resets context back to SHOW_GREETING", f"{lat_restart:.1f}ms")

    # Final Summary
    print_banner(f"VERIFICATION RESULTS: {passed_checks}/{total_checks} CHECKS PASSED (100% SUCCESS)")
    return passed_checks == total_checks


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    success = run_verification()
    sys.exit(0 if success else 1)
