"""Session management and state persistence for the Greenie kiosk.

Provides per-session tracking with configurable idle timeouts and thread resets
using LangGraph's native MemorySaver checkpointing or MongoDB persistent checkpointing.
"""

import logging
import time
from typing import Dict, Optional
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from app.config import settings

logger = logging.getLogger("greenie.sessions")


class SessionManager:
    """Manages session activity timestamps, idle timeouts, and thread resets."""

    def __init__(
        self,
        timeout_seconds: Optional[int] = None,
        checkpointer: Optional[BaseCheckpointSaver] = None,
    ) -> None:
        self.timeout_seconds: int = (
            timeout_seconds if timeout_seconds is not None else settings.session_idle_timeout_seconds
        )
        self._last_active: Dict[str, float] = {}
        self._session_owners: Dict[str, str] = {}
        if checkpointer is not None:
            self.checkpointer: BaseCheckpointSaver = checkpointer
        elif settings.checkpointer_backend == "mongodb":
            from app.database import get_greeny_ai_db
            from app.mongo_checkpointer import MongoCheckpointSaver
            from app.storage import DatabaseConnectionError
            try:
                db = get_greeny_ai_db()
                saver = MongoCheckpointSaver(db)
                # Index provisioning is an explicit deployment step, never a startup write.
                self.checkpointer = saver
                logger.info("SessionManager initialized with MongoCheckpointSaver.")
            except Exception as e:
                logger.error("Production MongoCheckpointSaver initialization failed: %s", type(e).__name__)
                raise DatabaseConnectionError(
                    f"Production MongoDB persistence initialization failed: {e}. Silent memory fallback is prohibited."
                ) from e
        else:
            self.checkpointer = MemorySaver()

    def set_checkpointer(self, checkpointer: BaseCheckpointSaver) -> None:
        """Dynamically replace the checkpointer instance."""
        self.checkpointer = checkpointer

    def get_session_kiosk(self, session_id: str) -> Optional[str]:
        """Return the owner kiosk_id for a given session_id if known."""
        if hasattr(self.checkpointer, "get_session_kiosk"):
            ckpt_owner = self.checkpointer.get_session_kiosk(session_id)
            if ckpt_owner:
                return ckpt_owner
        return self._session_owners.get(session_id)

    def touch(self, session_id: str, kiosk_id: Optional[str] = None) -> None:
        """Register activity for a session, resetting expired state if idle timeout exceeded."""
        from app.mongo_checkpointer import CrossKioskAccessError
        now = time.time()
        if hasattr(self.checkpointer, "touch_session"):
            self.checkpointer.touch_session(session_id, kiosk_id or "kiosk-default", self.timeout_seconds)
        last = self._last_active.get(session_id)
        if last is not None and (now - last) > self.timeout_seconds:
            # Session expired: reset checkpointer thread
            self.reset_session(session_id, kiosk_id=kiosk_id)

        if kiosk_id:
            owner = self.get_session_kiosk(session_id)
            if owner and owner != kiosk_id:
                raise CrossKioskAccessError(
                    f"Cross-kiosk access denied: Kiosk '{kiosk_id}' cannot access session owned by '{owner}'."
                )
            self._session_owners[session_id] = kiosk_id

        self._last_active[session_id] = now

    def is_expired(self, session_id: str) -> bool:
        """Check whether a session has exceeded the idle timeout."""
        last = self._last_active.get(session_id)
        if last is None:
            return False
        return (time.time() - last) > self.timeout_seconds

    def reset_session(self, session_id: str, kiosk_id: Optional[str] = None) -> bool:
        """Explicitly reset memory for a given session_id.

        Clears the LangGraph thread checkpoint and removes the timestamp.
        """
        from app.mongo_checkpointer import CrossKioskAccessError
        if kiosk_id:
            owner = self.get_session_kiosk(session_id)
            if owner and owner != kiosk_id:
                raise CrossKioskAccessError(
                    f"Cross-kiosk reset denied: Kiosk '{kiosk_id}' cannot reset session owned by '{owner}'."
                )

        existed = session_id in self._last_active
        if hasattr(self.checkpointer, "delete_thread"):
            try:
                from app.mongo_checkpointer import MongoCheckpointSaver
                if isinstance(self.checkpointer, MongoCheckpointSaver):
                    self.checkpointer.delete_thread(session_id, request_kiosk_id=kiosk_id)
                else:
                    self.checkpointer.delete_thread(session_id)
            except Exception as e:
                logger.error("Error deleting thread %s: %s", session_id, type(e).__name__)
                raise
        self._last_active.pop(session_id, None)
        # Keep the owner binding after reset; a known session ID is not transferable.
        return existed

    def get_active_sessions(self) -> Dict[str, float]:
        """Return currently tracked sessions and their last active timestamps."""
        return dict(self._last_active)


# Singleton session manager
session_manager = SessionManager()
