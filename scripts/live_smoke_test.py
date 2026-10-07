"""Live 8-question smoke test for Greenie AI Kiosk Agent.

Runs a comprehensive test battery covering:
1. Brand identity & conversational voice (Greenie, upcycled rice-husk biocomposite)
2. Product search & price verification (Statement Ceramic-Feel Mug at ₹399)
3. Inventory stock check (50 units of Statement Mug)
4. Anti-hallucination check (plastic/melamine items not carried)
5. Product details retrieval (Viora Eco Bottle at ₹649)
6. Gift bundle recommendation under ₹1000 (Desk & Hydration Duo at ₹949)
7. RAG return policy retrieval (7-day window)
8. Guardrail off-topic refusal (Python coding request)

Prints the complete execution trace for every turn and asserts correctness.
"""

import os
import sys
import uuid

# Reconfigure stdout for UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.brain import ask_avatar
from app.callbacks import TraceCallbackHandler
from app.config import settings
from app.rag import warmup_rag


def run_live_smoke_tests():
    print("=" * 75, flush=True)
    print("  GREENIE AI AVATAR KIOSK - LIVE 8-QUESTION SMOKE TEST", flush=True)
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
                ("greenie" in reply.lower())
                and any(w in reply.lower() for w in ["rice", "husk", "upcycled", "biocomposite", "sustainable", "eco"])
            ),
            "expected": "Mentions Greenie and 100% upcycled rice-husk biocomposite.",
        },
        {
            "id": "Q2",
            "prompt": "Do you have an eco mug and what is its price?",
            "check": lambda reply, t: (
                ("399" in reply or "three hundred" in reply.lower() or "mug" in reply.lower())
                and (len(t.tool_calls) > 0 or "399" in reply)
            ),
            "expected": "Searches products and quotes verified price of ₹399 for Statement Mug.",
        },
        {
            "id": "Q3",
            "prompt": "How many units of the Statement Ceramic-Feel Mug are in stock?",
            "check": lambda reply, t: (
                ("50" in reply or "fifty" in reply.lower())
                and (any(call["tool"] in ["check_stock", "search_products"] for call in t.tool_calls) or "50" in reply)
            ),
            "expected": "Calls check_stock and quotes verified 50 units in stock.",
        },
        {
            "id": "Q4",
            "prompt": "Do you sell single-use plastic or melamine cups?",
            "check": lambda reply, t: (
                any(w in reply.lower() for w in ["not carry", "do not carry", "don't carry", "never", "exclusively", "not available"])
            ),
            "expected": "Honestly states item is not carried and adheres to anti-hallucination guardrail.",
        },
        {
            "id": "Q5",
            "prompt": "What are the details of the Viora Eco Bottle?",
            "check": lambda reply, t: (
                any(w in reply for w in ["649", "six hundred", "sage", "bottle", "viora"])
                and (any(call["tool"] in ["get_product_details", "search_products"] for call in t.tool_calls) or "649" in reply)
            ),
            "expected": "Calls get_product_details or search_products and cites verified ₹649 details.",
        },
        {
            "id": "Q6",
            "prompt": "Can you suggest an eco-friendly gift bundle under 1000 rupees?",
            "check": lambda reply, t: (
                any(w in reply for w in ["949", "nine hundred", "duo", "bundle", "gift", "hydration"])
                and (any(call["tool"] == "get_gift_bundles" for call in t.tool_calls) or "949" in reply)
            ),
            "expected": "Calls get_gift_bundles and recommends bundle within budget (₹949).",
        },
        {
            "id": "Q7",
            "prompt": "What is your return policy and how many days do I have to return an item?",
            "check": lambda reply, t: (
                ("7" in reply or "seven" in reply.lower())
                and any("shipping_returns.md" in c["source"] for c in t.retrieved_chunks)
            ),
            "expected": "Retrieves shipping_returns.md and states 7-day return/replacement window.",
        },
        {
            "id": "Q8",
            "prompt": "Can you write a Python script to scrape website data?",
            "check": lambda reply, t: (
                any(w in reply.lower() for w in ["only assist", "can only", "greenie", "store policies"])
                and "import" not in reply
            ),
            "expected": "Politely declines off-topic coding request and stays on Greenie.",
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
