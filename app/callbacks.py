"""Callback handlers for observability and tracing during avatar conversational turns.

Tracks active LLM provider/model, latency, retrieved RAG chunks, and tool execution traces.
"""

import time
from typing import Any, Dict, List, Optional
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from app.config import settings
from app.privacy import redact_contacts


class TraceCallbackHandler(BaseCallbackHandler):
    """Observability handler recording timing, RAG context, and tool invocations per turn."""

    def __init__(self) -> None:
        super().__init__()
        self.provider: str = settings.llm_provider
        self.model: str = settings.active_model_name
        self.turn_start_time: float = 0.0
        self.turn_end_time: float = 0.0
        self.llm_start_time: float = 0.0
        self.llm_end_time: float = 0.0
        self.retrieved_chunks: List[Dict[str, Any]] = []
        self.tool_calls: List[Dict[str, Any]] = []
        self.token_count: int = 0
        self.error: Optional[str] = None
        self._current_tool_start: float = 0.0

    def start_turn(self) -> None:
        """Mark the start of a user interaction turn."""
        self.turn_start_time = time.time()
        self.turn_end_time = 0.0
        self.retrieved_chunks = []
        self.tool_calls = []
        self.token_count = 0
        self.error = None
        self.provider = settings.llm_provider
        self.model = settings.active_model_name

    def end_turn(self) -> None:
        """Mark the completion of a user interaction turn."""
        self.turn_end_time = time.time()

    def record_retrieved_chunks(self, chunks: List[Dict[str, Any]]) -> None:
        """Store RAG chunks retrieved during the turn."""
        self.retrieved_chunks = redact_contacts(chunks)

    def on_llm_start(
        self, serialized: Dict[str, Any], prompts: List[str], **kwargs: Any
    ) -> None:
        self.llm_start_time = time.time()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        self.llm_end_time = time.time()

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self.error = type(error).__name__

    def on_tool_start(
        self, serialized: Dict[str, Any], input_str: str, **kwargs: Any
    ) -> None:
        self._current_tool_start = time.time()
        tool_name = serialized.get("name", "tool")
        self.tool_calls.append({
            "tool": tool_name,
            "input": redact_contacts(input_str),
            "result": None,
            "duration_ms": 0.0,
        })

    def on_tool_end(self, output: str, **kwargs: Any) -> None:
        duration_ms = (time.time() - self._current_tool_start) * 1000.0
        if self.tool_calls:
            self.tool_calls[-1]["result"] = redact_contacts(output)
            self.tool_calls[-1]["duration_ms"] = round(duration_ms, 2)

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        duration_ms = (time.time() - self._current_tool_start) * 1000.0
        if self.tool_calls:
            self.tool_calls[-1]["result"] = f"[Error] {type(error).__name__}"
            self.tool_calls[-1]["duration_ms"] = round(duration_ms, 2)

    @property
    def total_turn_duration_ms(self) -> float:
        end = self.turn_end_time if self.turn_end_time > 0 else time.time()
        start = self.turn_start_time if self.turn_start_time > 0 else end
        return round((end - start) * 1000.0, 2)

    def render_trace(self) -> str:
        """Format the recorded trace information into a clean terminal report."""
        lines = [
            "=" * 70,
            "  TURN TRACE REPORT",
            "=" * 70,
            f"Provider       : {self.provider.upper()}",
            f"Model          : {self.model}",
            f"Turn Latency   : {self.total_turn_duration_ms} ms",
        ]

        if self.retrieved_chunks:
            lines.append(f"\nRetrieved Knowledge Chunks ({len(self.retrieved_chunks)}):")
            for i, c in enumerate(self.retrieved_chunks, 1):
                preview = c.get("content", "").strip().replace("\n", " ")[:90]
                lines.append(
                    f"  [{i}] Source: {c.get('source')} | Score: {c.get('score', 0):.4f}"
                )
                lines.append(f"      \"{preview}...\"")
        else:
            lines.append("\nRetrieved Knowledge Chunks : None")

        if self.tool_calls:
            lines.append(f"\nTool Invocations ({len(self.tool_calls)}):")
            for i, t in enumerate(self.tool_calls, 1):
                lines.append(
                    f"  [{i}] Tool: {t['tool']} ({t['duration_ms']} ms)"
                )
                lines.append(f"      Args   : {t['input']}")
                res_preview = str(t['result']).strip().replace("\n", " ")[:100]
                lines.append(f"      Result : {res_preview}")
        else:
            lines.append("Tool Invocations           : None")

        if self.error:
            lines.append(f"\nError Recorded : {self.error}")

        lines.append("=" * 70)
        return "\n".join(lines)
