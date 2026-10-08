"""Session management and state persistence for the Greenie kiosk.

Provides per-session tracking with configurable idle timeouts and thread resets
using LangGraph's native MemorySaver checkpointing.
"""

import time
from typing import Dict, Optional
from langgraph.checkpoint.memory import MemorySaver
from app.config import settings


class SessionManager:
    """Manages session activity timestamps, idle timeouts, and thread resets."""

    def __init__(self, timeout_seconds: Optional[int] = None) -> None:
        self.timeout_seconds: int = (
            timeout_seconds if timeout_seconds is not None else settings.session_idle_timeout_seconds
        )
        self.checkpointer: MemorySaver = MemorySaver()
        self._last_active: Dict[str, float] = {}

    def touch(self, session_id: str) -> None:
        """Register activity for a session, resetting expired state if idle timeout exceeded."""
        now = time.time()
        last = self._last_active.get(session_id)
        if last is not None and (now - last) > self.timeout_seconds:
            # Session expired: reset checkpointer thread
            self.reset_session(session_id)
        self._last_active[session_id] = now

    def is_expired(self, session_id: str) -> bool:
        """Check whether a session has exceeded the idle timeout."""
        last = self._last_active.get(session_id)
        if last is None:
            return False
        return (time.time() - last) > self.timeout_seconds

    def reset_session(self, session_id: str) -> bool:
        """Explicitly reset memory for a given session_id.
        
        Clears the LangGraph thread checkpoint and removes the timestamp.
        """
        existed = session_id in self._last_active
        # Preserve activity metadata and propagate failure if deletion fails.
        self.checkpointer.delete_thread(session_id)
        self._last_active.pop(session_id, None)
        return existed

    def get_active_sessions(self) -> Dict[str, float]:
        """Return currently tracked sessions and their last active timestamps."""
        return dict(self._last_active)


# Singleton session manager
session_manager = SessionManager()
