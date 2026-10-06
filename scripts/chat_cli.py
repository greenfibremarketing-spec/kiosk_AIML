"""Interactive terminal chat CLI for testing the Green Fibre AI Avatar Brain.

Allows testing session memory, spoken persona, and guardrails directly in the terminal.
"""

import os
import sys

# Ensure repository root is in python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import uuid
from app.brain import ask_avatar
from app.sessions import session_manager


def main():
    session_id = f"cli-{uuid.uuid4().hex[:6]}"
    print("=" * 65)
    print("  GREEN FIBRE AI AVATAR KIOSK - CLI CHAT")
    print("=" * 65)
    print(f"Session ID : {session_id}")
    print("Commands   : '/reset' to clear memory | '/session <id>' to switch")
    print("             '/exit' to quit")
    print("=" * 65)
    print("Avatar is listening. Type your message below:\n")

    while True:
        try:
            user_input = input("You: ").strip()
            if not user_input:
                continue

            if user_input.lower() in ["/exit", "exit", "quit", "q"]:
                print("\nAvatar: Thank you for visiting Green Fibre! Have a wonderful day.")
                break

            if user_input.lower() == "/reset":
                session_manager.reset_session(session_id)
                print(f"[System] Memory cleared for session: {session_id}\n")
                continue

            if user_input.startswith("/session "):
                parts = user_input.split(" ", 1)
                if len(parts) > 1 and parts[1].strip():
                    session_id = parts[1].strip()
                    print(f"[System] Switched to session: {session_id}\n")
                continue

            # Send message through the LangGraph brain with session memory
            spoken_text = ask_avatar(user_input, session_id=session_id)
            print(f"\nAvatar: {spoken_text}\n")

        except (KeyboardInterrupt, EOFError):
            print("\n\nAvatar: Goodbye! Have a green day.")
            break
        except Exception as e:
            print(f"\n[Error] {str(e)}\n")


if __name__ == "__main__":
    main()
