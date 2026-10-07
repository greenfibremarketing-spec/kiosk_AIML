"""Interactive terminal chat CLI for testing the Greenie AI Avatar Brain.

Supports session memory, spoken persona, guardrails, RAG chunks inspection,
and real-time turn tracing via the --trace flag or /trace command.
"""

import argparse
import os
import sys

# Ensure repository root is in python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import uuid
from app.brain import ask_avatar
from app.callbacks import TraceCallbackHandler
from app.config import settings
from app.rag import print_retrieval_inspect
from app.sessions import session_manager


def main():
    parser = argparse.ArgumentParser(description="Greenie AI Avatar Kiosk CLI")
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Print detailed execution trace (latency, provider/model, RAG chunks, tools) for each turn",
    )
    args = parser.parse_args()

    show_trace = args.trace
    session_id = f"cli-{uuid.uuid4().hex[:6]}"
    trace_handler = TraceCallbackHandler() if show_trace else None

    print("=" * 65)
    print("  GREENIE AI AVATAR KIOSK - CLI CHAT")
    print("=" * 65)
    print(f"Session ID : {session_id}")
    print(f"LLM Provider: {settings.llm_provider.upper()} ({settings.active_model_name})")
    print(f"Tracing    : {'ENABLED' if show_trace else 'DISABLED'} (toggle with '/trace')")
    print("Commands   : '/reset' to clear memory | '/trace' to toggle trace")
    print("             '/chunks <query>' to inspect RAG retrieved chunks")
    print("             '/exit' to quit")
    print("=" * 65)
    print("Avatar is listening. Type your message below:\n")

    while True:
        try:
            user_input = input("You: ").strip()
            if not user_input:
                continue

            if user_input.lower() in ["/exit", "exit", "quit", "q"]:
                print("\nAvatar: Thank you for visiting Greenie! Have a wonderful day.")
                break

            if user_input.lower() == "/reset":
                session_manager.reset_session(session_id)
                print(f"[System] Memory cleared for session: {session_id}\n")
                continue

            if user_input.lower() == "/trace":
                show_trace = not show_trace
                trace_handler = TraceCallbackHandler() if show_trace else None
                print(f"[System] Trace reporting: {'ENABLED' if show_trace else 'DISABLED'}\n")
                continue

            if user_input.startswith("/session "):
                parts = user_input.split(" ", 1)
                if len(parts) > 1 and parts[1].strip():
                    session_id = parts[1].strip()
                    print(f"[System] Switched to session: {session_id}\n")
                continue

            if user_input.startswith("/chunks "):
                query = user_input[len("/chunks "):].strip()
                if query:
                    print_retrieval_inspect(query)
                else:
                    print("[System] Usage: /chunks <question or topic>\n")
                continue

            # Send message through the LangGraph brain with session memory & optional trace
            spoken_text = ask_avatar(
                user_input,
                session_id=session_id,
                trace_handler=trace_handler,
            )
            print(f"\nAvatar: {spoken_text}\n")

            if show_trace and trace_handler:
                print(trace_handler.render_trace())
                print()

        except (KeyboardInterrupt, EOFError):
            print("\n\nAvatar: Goodbye! Have a green day.")
            break
        except Exception as e:
            print(f"\n[Error] {str(e)}\n")


if __name__ == "__main__":
    main()
