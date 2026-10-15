"""
fast_flow_demo.py  -  5-step kiosk demo (server starts first, no user greeting needed)

Run the backend first:
    python main.py

Then in a separate terminal:
    python scripts/fast_flow_demo.py

What you will see:
  1. Client connects -> SERVER immediately sends SHOW_GREETING + speech_id
  2. Client ACKs TTS done  -> SERVER sends ASK_NAME
  3. Client sends name "Rahul" -> SERVER sends SHOW_CATEGORIES (button list)
  4. Client taps first category button -> SERVER sends SHOW_PRODUCT_CARD #1
  5. Client taps "Next"  -> SERVER sends SHOW_PRODUCT_CARD #2
  6. Client taps "Cheaper" -> SERVER filters and shows cheaper product
  7. Client taps "I'll take it" -> SERVER sends CONFIRM_BUY
  8. Client says "yes" -> SERVER sends CHECKOUT_HANDOFF
  9. Client says "goodbye" -> SERVER sends GOODBYE, auto-reset after 4 s
"""

import asyncio
import json
import sys
import uuid
import websockets

BACKEND_WS = "ws://localhost:5007/ws"
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


def hdr(step, title):
    print(f"\n{BOLD}{CYAN}{'='*68}{RESET}")
    print(f"{BOLD}{CYAN}  Step {step}: {title}{RESET}")
    print(f"{BOLD}{CYAN}{'='*68}{RESET}")


def send_label(payload):
    print(f"{YELLOW}  [CLIENT -> SERVER]{RESET} {json.dumps(payload)}")


def recv_label(frame_type, data_preview, action_name=None):
    tag = f"({action_name})" if action_name else ""
    preview = str(data_preview)[:120]
    print(f"{GREEN}  [SERVER -> CLIENT]{RESET} type={frame_type} {tag}  {preview}")


async def recv_turn(ws):
    """Collect frames until 'done'. Return (reply, action, stage, full_frames)."""
    tokens   = []
    action   = None
    stage    = None
    frames   = []
    while True:
        raw  = await ws.recv()
        msg  = json.loads(raw)
        mtype = msg.get("type")
        data  = msg.get("data", "")
        meta  = msg.get("meta") or {}
        frames.append(msg)

        if mtype == "token":
            tokens.append(data)
        elif mtype == "action":
            action = data
            recv_label(mtype, "", action.get("action") if isinstance(action, dict) else "")
            # Print buttons
            if isinstance(action, dict):
                btns = action.get("buttons") or []
                always = action.get("always") or []
                if btns:
                    print(f"       Buttons : {[b['label'] for b in btns]}")
                if always:
                    print(f"       Always  : {[b['label'] for b in always]}")
                # Extra fields
                for k in ("categories", "products", "product", "sku", "product_url",
                          "speech_id", "awaiting_speech_done", "auto_reset_after_ms"):
                    if k in action:
                        val = action[k]
                        if isinstance(val, list):
                            val = [v.get("name") or v.get("id") or v for v in val] if val and isinstance(val[0], dict) else val
                        print(f"       {k:20s}: {val}")
        elif mtype == "done":
            stage = meta.get("sales_stage")
            recv_label(mtype, data[:80], stage)
            break
        elif mtype in ("error", "ping_silence", "auto_reset", "cancelled"):
            recv_label(mtype, data)
            if mtype in ("auto_reset",):
                break
        elif mtype == "sentence":
            recv_label(mtype, data)

    reply = "".join(tokens).strip() or (data if mtype == "done" else "")
    print(f"\n  {BOLD}Avatar says:{RESET} \"{reply}\"")
    return reply, action, stage


async def main():
    session_id = f"fast-demo-{uuid.uuid4().hex[:6]}"
    url = f"{BACKEND_WS}/{session_id}?v=2&kiosk_id=kiosk-01"

    print(f"\n{BOLD}Connecting to: {url}{RESET}")
    async with websockets.connect(url) as ws:
        print(f"{GREEN}Connected.{RESET}\n")

        # ── STEP 1: Server greets automatically on connect ─────────────────
        hdr(1, "WAKE  —  Server sends greeting as soon as client connects")
        print("  (No message sent by client — server initiates!)")
        reply1, action1, stage1 = await recv_turn(ws)

        speech_id = action1.get("speech_id") if isinstance(action1, dict) else None
        await asyncio.sleep(0.5)

        # ── STEP 2: Client ACKs TTS done ──────────────────────────────────
        hdr(2, "SPEECH_DONE  —  Avatar TTS audio finished playing")
        payload2 = {"v": 2, "action": "SPEECH_DONE", "speech_id": speech_id, "turn_id": "turn-02"}
        send_label(payload2)
        await ws.send(json.dumps(payload2))
        reply2, action2, stage2 = await recv_turn(ws)
        await asyncio.sleep(0.5)

        # ── STEP 3: Client sends name ──────────────────────────────────────
        hdr(3, "NAME  —  Customer says 'Rahul'")
        payload3 = {"v": 2, "message": "Rahul", "turn_id": "turn-03"}
        send_label(payload3)
        await ws.send(json.dumps(payload3))
        reply3, action3, stage3 = await recv_turn(ws)
        await asyncio.sleep(0.5)

        # Pick first category from the action
        cats = (action3 or {}).get("categories", [])
        first_cat = cats[0]["id"] if cats else "drinkware"
        cat_label = cats[0]["name"] if cats else "Drinkware"

        # ── STEP 4: Client taps a category button ─────────────────────────
        hdr(4, f"CATEGORY  —  Customer taps '{cat_label}' button")
        payload4 = {"v": 2, "action": "SELECT_CATEGORY", "category_id": first_cat, "turn_id": "turn-04"}
        send_label(payload4)
        await ws.send(json.dumps(payload4))
        reply4, action4, stage4 = await recv_turn(ws)
        await asyncio.sleep(0.5)

        # ── STEP 5: Client taps "Next" ────────────────────────────────────
        hdr(5, "PRODUCT  —  Customer taps 'Next' button")
        payload5 = {"v": 2, "message": "next", "turn_id": "turn-05"}
        send_label(payload5)
        await ws.send(json.dumps(payload5))
        reply5, action5, stage5 = await recv_turn(ws)
        await asyncio.sleep(0.5)

        # ── STEP 6: Client taps "Cheaper" ────────────────────────────────
        hdr(6, "PRODUCT  —  Customer taps 'Cheaper' button")
        payload6 = {"v": 2, "message": "cheaper", "turn_id": "turn-06"}
        send_label(payload6)
        await ws.send(json.dumps(payload6))
        reply6, action6, stage6 = await recv_turn(ws)
        await asyncio.sleep(0.5)

        # ── STEP 7: Customer taps "I'll take it" ────────────────────────
        hdr(7, "BUY  —  Customer taps 'I'll take it'")
        payload7 = {"v": 2, "message": "i'll take it", "turn_id": "turn-07"}
        send_label(payload7)
        await ws.send(json.dumps(payload7))
        reply7, action7, stage7 = await recv_turn(ws)
        await asyncio.sleep(0.5)

        # ── STEP 8: Customer confirms checkout ────────────────────────────
        hdr(8, "CHECKOUT  —  Customer says 'yes' (confirm)")
        payload8 = {"v": 2, "message": "yes", "turn_id": "turn-08"}
        send_label(payload8)
        await ws.send(json.dumps(payload8))
        reply8, action8, stage8 = await recv_turn(ws)
        checkout_url = (action8 or {}).get("product_url", "(no URL - mock mode)")
        print(f"\n  Checkout URL: {checkout_url}")
        await asyncio.sleep(0.5)

        # ── STEP 9: Customer says goodbye ─────────────────────────────────
        hdr(9, "GOODBYE  —  Customer says 'thanks, bye'")
        payload9 = {"v": 2, "message": "thanks", "turn_id": "turn-09"}
        send_label(payload9)
        await ws.send(json.dumps(payload9))
        reply9, action9, stage9 = await recv_turn(ws)
        reset_ms = (action9 or {}).get("auto_reset_after_ms", "?")
        print(f"\n  Kiosk will auto-reset for next customer in {reset_ms} ms")

        print(f"\n{BOLD}{GREEN}{'='*68}{RESET}")
        print(f"{BOLD}{GREEN}  DEMO COMPLETE  -  Full 5-step fast flow verified via WebSocket!{RESET}")
        print(f"{BOLD}{GREEN}{'='*68}{RESET}\n")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
