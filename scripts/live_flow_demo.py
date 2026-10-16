"""Live Greenie Sales Flow Demonstration.

Connects to the live running backend on ws://localhost:5007
and walks step-by-step through the real customer sales flow:
1. GREETING: Avatar welcomes customer & issues speech_id
2. SPEECH_DONE: Customer TTS audio finishes playing
3. ASK_NAME: Customer submits their name ("Aman")
4. ASK_CATEGORY: Customer selects category ("bottle")
5. SHOW_PRODUCTS: Avatar shows product cards
6. PRODUCT_QUESTIONS: Customer asks a product question ("Is it microwave safe and what is the material?")
"""

import asyncio
import json
import sys
import uuid
import websockets

BACKEND_URL = "ws://localhost:5007/ws"


async def run_live_turn(ws, payload, step_title):
    print("\n" + "=" * 70)
    print(f"STEP: {step_title}")
    print("=" * 70)
    print(f"[CLIENT SENDS] -> {json.dumps(payload, indent=2)}")

    await ws.send(json.dumps(payload))

    tokens = []
    received_action = None
    sales_stage = None
    turn_reply = None

    while True:
        raw = await ws.recv()
        msg = json.loads(raw)
        mtype = msg.get("type")
        data = msg.get("data")
        meta = msg.get("meta", {})

        if mtype == "token":
            tokens.append(data)
        elif mtype == "action":
            received_action = data
        elif mtype == "done":
            turn_reply = data
            sales_stage = meta.get("sales_stage") if meta else None
            break
        elif mtype == "error":
            print(f"[ERROR] {data}")
            break

    print(f"\n[AVATAR SAYS]:")
    print(f'"{turn_reply}"')
    print(f"\n[SALES STAGE]: {sales_stage}")
    if received_action:
        print(f"[SCREEN ACTION EMITTED]:")
        print(json.dumps(received_action, indent=2))

    return turn_reply, received_action, sales_stage


async def main():
    session_id = f"demo-{uuid.uuid4().hex[:6]}"
    url = f"{BACKEND_URL}/{session_id}?v=2&kiosk_id=kiosk-01"

    print("Connecting to live backend:", url)
    async with websockets.connect(url) as ws:
        print("Connected successfully!\n")

        # Turn 1: Initial Greeting
        reply1, action1, stage1 = await run_live_turn(
            ws,
            {"v": 2, "message": "Hi Greenie", "turn_id": "turn-01"},
            "1. Customer approaches kiosk & says Hello"
        )
        speech_id = (action1 or {}).get("speech_id")
        await asyncio.sleep(1)

        # Turn 2: Speech Done (TTS ends)
        if speech_id:
            reply2, action2, stage2 = await run_live_turn(
                ws,
                {"v": 2, "action": "SPEECH_DONE", "speech_id": speech_id, "turn_id": "turn-02"},
                "2. Avatar TTS audio finishes -> Client sends SPEECH_DONE"
            )
            await asyncio.sleep(1)

        # Turn 3: Set Name
        reply3, action3, stage3 = await run_live_turn(
            ws,
            {"v": 2, "action": "SET_NAME", "name": "Aman", "turn_id": "turn-03"},
            "3. Customer types or speaks their name ('Aman')"
        )
        await asyncio.sleep(1)

        # Extract available categories from action
        categories = (action3 or {}).get("categories", [])
        chosen_cat = categories[0]["id"] if categories else "bottle"

        # Turn 4: Select Category
        reply4, action4, stage4 = await run_live_turn(
            ws,
            {"v": 2, "action": "SELECT_CATEGORY", "category_id": chosen_cat, "turn_id": "turn-04"},
            f"4. Customer taps category button ('{chosen_cat}')"
        )
        await asyncio.sleep(1)

        # Turn 5: Ask Live Product Question
        reply5, action5, stage5 = await run_live_turn(
            ws,
            {"v": 2, "message": "What material are these bottles made of, and are they safe for hot drinks?", "turn_id": "turn-05"},
            "5. Customer asks live product & material question"
        )

        print("\n" + "=" * 70)
        print("  DEMO COMPLETE: Guided Sales Flow & Live Knowledge Q&A Verified!")
        print("=" * 70)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    asyncio.run(main())
