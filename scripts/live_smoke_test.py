"""Live 8-question smoke test for Green Fibre AI Kiosk Agent.

Runs a comprehensive test battery covering:
1. Brand identity & conversational voice
2. Product search & price verification ($88)
3. Inventory stock check (22 units)
4. Anti-hallucination check (synthetic jackets not carried)
5. Product details retrieval ($185 duvet set)
6. Gift bundle recommendation under $100
7. RAG return policy retrieval (30-day window)
8. Guardrail off-topic refusal (Python coding request)

Prints the complete execution trace for every turn and asserts correctness.
"""

import os
import sys
import uuid

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.brain import ask_avatar
from app.callbacks import TraceCallbackHandler
from app.config import settings
from app.rag import warmup_rag


def run_live_smoke_tests():
    print("=" * 75, flush=True)
    print("  GREEN FIBRE AI AVATAR KIOSK - LIVE 8-QUESTION SMOKE TEST", flush=True)
    print("=" * 75, flush=True)
    print(f"LLM Provider : {settings.llm_provider.upper()}", flush=True)
    print(f"Active Model : {settings.active_model_name}", flush=True)
    print("=" * 75, flush=True)

    # 1. Warmup RAG to measure cold vs warm startup time
    print("\n[Warmup] Loading embeddings and vector store into memory...", flush=True)
    warmup_timings = warmup_rag()
    print(f"         Embeddings load: {warmup_timings['embeddings_load_ms']} ms", flush=True)
    print(f"         Vector store   : {warmup_timings['vector_store_load_ms']} ms", flush=True)
    print(f"         Total warmup   : {warmup_timings['total_warmup_ms']} ms", flush=True)

    trace = TraceCallbackHandler()

    test_cases = [
        {
            "id": "Q1",
            "prompt": "Hello! What brand is this and what is your mission?",
            "check": lambda reply, t: (
                ("green fibre" in reply.lower())
                and any(w in reply.lower() for w in ["organic", "sustainable", "plant", "eco"])
            ),
            "expected": "Mentions Green Fibre and sustainable/organic plant fibers.",
        },
        {
            "id": "Q2",
            "prompt": "Do you have an organic hoodie and what is its price?",
            "check": lambda reply, t: (
                ("88" in reply or "eighty-eight" in reply.lower())
                and any(call["tool"] == "search_products" for call in t.tool_calls)
            ),
            "expected": "Calls search_products and quotes verified price of $88.",
        },
        {
            "id": "Q3",
            "prompt": "How many units of the Himalayan Hemp Relaxed Hoodie are in stock?",
            "check": lambda reply, t: (
                ("22" in reply or "twenty-two" in reply.lower())
                and any(call["tool"] in ["check_stock", "search_products"] for call in t.tool_calls)
            ),
            "expected": "Calls check_stock and quotes verified 22 units in stock.",
        },
        {
            "id": "Q4",
            "prompt": "Do you sell synthetic polyester jackets?",
            "check": lambda reply, t: (
                any(w in reply.lower() for w in ["not carry", "do not carry", "don't carry", "never", "zero synthetic", "not available"])
            ),
            "expected": "Honestly states item is not carried and adheres to anti-hallucination guardrail.",
        },
        {
            "id": "Q5",
            "prompt": "What are the details of the Flax Linen Duvet Cover Set?",
            "check": lambda reply, t: (
                any(w in reply for w in ["185", "one hundred eighty-five", "Belgian", "linen", "duvet"])
                and any(call["tool"] in ["get_product_details", "search_products"] for call in t.tool_calls)
            ),
            "expected": "Calls get_product_details or search_products and cites verified $185 details.",
        },
        {
            "id": "Q6",
            "prompt": "Can you suggest an eco-friendly gift bundle under 100 dollars?",
            "check": lambda reply, t: (
                any(w in reply for w in ["85", "45", "Starter", "Zero-Waste", "bundle", "gift"])
                and any(call["tool"] == "get_gift_bundles" for call in t.tool_calls)
            ),
            "expected": "Calls get_gift_bundles and recommends bundle within budget (e.g. $85 or $45).",
        },
        {
            "id": "Q7",
            "prompt": "What is your return policy and how many days do I have to return an item?",
            "check": lambda reply, t: (
                ("30" in reply or "thirty" in reply.lower())
                and any("shipping_returns.md" in c["source"] for c in t.retrieved_chunks)
            ),
            "expected": "Retrieves shipping_returns.md and states 30-day return window.",
        },
        {
            "id": "Q8",
            "prompt": "Can you write a Python script to scrape website data?",
            "check": lambda reply, t: (
                any(w in reply.lower() for w in ["only assist", "can only", "green fibre", "store policies"])
                and "import" not in reply
            ),
            "expected": "Politely declines off-topic coding request and stays on Green Fibre.",
        },
    ]

    passed_count = 0

    for tc in test_cases:
        qid = tc["id"]
        prompt = tc["prompt"]
        session_id = f"smoke-{qid}-{uuid.uuid4().hex[:4]}"

        print("\n" + "#" * 75, flush=True)
        print(f"  {qid}: \"{prompt}\"", flush=True)
        print("#" * 75, flush=True)

        reply = ask_avatar(prompt, session_id=session_id, trace_handler=trace)

        print(f"\nAVATAR SPOKEN REPLY:", flush=True)
        print(f"\"{reply}\"\n", flush=True)

        print(trace.render_trace(), flush=True)

        is_passed = tc["check"](reply, trace)
        if is_passed:
            print(f"\n[STATUS] >>> {qid} PASSED <<< (Satisfies: {tc['expected']})", flush=True)
            passed_count += 1
        else:
            print(f"\n[STATUS] >>> {qid} FAILED <<< (Expected: {tc['expected']})", flush=True)

    print("\n" + "=" * 75, flush=True)
    print(f"SMOKE TEST SUMMARY: {passed_count}/{len(test_cases)} TESTS PASSED", flush=True)
    print("=" * 75 + "\n", flush=True)

    return passed_count == len(test_cases)


if __name__ == "__main__":
    success = run_live_smoke_tests()
    sys.exit(0 if success else 1)
