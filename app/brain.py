"""LangChain & LangGraph brain for the Green Fibre AI Avatar Kiosk.

Constructs the conversational engine using modern LangGraph StateGraph,
spoken avatar guardrails, and thread-based memory checkpointing.
"""

import re
from typing import Any, Dict, Iterator, List, Optional

# LangChain Core imports
# BaseChatModel: Standard abstract base class for all LangChain chat models.
from langchain_core.language_models.chat_models import BaseChatModel
# AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage: Standard message types.
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage
# ChatGeneration, ChatGenerationChunk, ChatResult: Response containers for chat model outputs.
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

# LangGraph imports
# StateGraph & MessagesState: Modern standard graph-based orchestration for multi-turn conversations and agents.
from langgraph.graph import StateGraph, START, END, MessagesState

from app.config import settings
from app.sessions import session_manager


# Spoken avatar guardrails in system prompt
SYSTEM_PROMPT = """You are the friendly, voice-first AI avatar assistant at the Green Fibre kiosk.
Green Fibre is an eco-friendly e-commerce brand specializing in sustainable apparel and home goods made from organic cotton, linen, hemp, and bamboo.

STRICT VOICE & SPOKEN RULES:
1. Your responses will be read out loud by a text-to-speech engine. Speak in natural, warm, and concise spoken English.
2. Keep your replies short: 1 to 3 sentences maximum.
3. NEVER use markdown formatting: no asterisks, no bolding, no headings, no code blocks, no backticks.
4. NEVER use bullet points or numbered lists. Use flowing sentences instead.
5. NEVER use emojis.

GUARDRAILS & BOUNDARIES:
1. Stay focused exclusively on Green Fibre: products, sustainable materials, order status, shipping, returns, and brand mission.
2. If asked about unrelated topics (such as politics, coding, general trivia, or competitors), politely decline: "I can only assist with Green Fibre products and questions."
3. Never invent facts, prices, discounts, or stock levels. If you do not have verified information, honestly state that you do not know.
4. Never ask for or store sensitive personal information such as passwords, credit card numbers, or full payment details.
"""


def clean_spoken_text(text: str) -> str:
    """Sanitize model output to ensure plain spoken sentences for TTS.
    
    Removes markdown markers, asterisks, hash headers, bullet dashes, and emojis.
    """
    if not text:
        return ""
    # Strip markdown headers (e.g., # or ##)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    # Strip bold / italics markdown (* or _)
    text = re.sub(r"[\*_]{1,3}(.*?)[*_]{1,3}", r"\1", text)
    # Strip bullet point markers (- or * or •) at the start of lines
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)
    # Strip backticks
    text = text.replace("`", "")
    # Strip emojis (Unicode ranges)
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "]+",
        flags=re.UNICODE,
    )
    text = emoji_pattern.sub("", text)
    # Normalize excessive whitespace and line breaks
    text = re.sub(r"\s+", " ", text).strip()
    return text


class MockKioskChatModel(BaseChatModel):
    """Realistic offline fallback chat model for kiosk development and testing.
    
    Generates spoken, concise, brand-aware responses without requiring an Anthropic API key.
    """

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[Any] = None,
        **kwargs: Any,
    ) -> ChatResult:
        last_message = ""
        for m in reversed(messages):
            if isinstance(m, HumanMessage):
                last_message = m.content.strip()
                break
        text = self._generate_response_text(last_message, messages)
        generation = ChatGeneration(message=AIMessage(content=text))
        return ChatResult(generations=[generation])

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[Any] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        last_message = ""
        for m in reversed(messages):
            if isinstance(m, HumanMessage):
                last_message = m.content.strip()
                break
        full_text = self._generate_response_text(last_message, messages)
        # Yield word-by-word with spaces to emulate realistic token streaming
        words = full_text.split(" ")
        for i, word in enumerate(words):
            chunk_text = word if i == len(words) - 1 else word + " "
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=chunk_text))
            if run_manager:
                run_manager.on_llm_new_token(chunk_text)
            yield chunk

    def _generate_response_text(self, query: str, history: List[BaseMessage]) -> str:
        q = query.lower()
        # Greetings
        if any(w in q for w in ["hi", "hello", "hey", "good morning", "good afternoon"]):
            return "Hello! Welcome to Green Fibre. How can I help you discover our eco-friendly collection today?"

        # Off-topic checks
        if any(w in q for w in ["python", "code", "weather", "president", "math", "bitcoin"]):
            return "I can only assist with Green Fibre products, sustainable materials, and store policies."

        # Brand / Mission
        if any(w in q for w in ["brand", "story", "mission", "green fibre", "who are you"]):
            return "Green Fibre is dedicated to sustainable living with garments and home essentials made from organic cotton, linen, hemp, and bamboo."

        # Shipping & returns
        if "shipping" in q or "delivery" in q:
            return "We provide carbon-neutral shipping on all orders, with free delivery on purchases over fifty dollars."
        if "return" in q or "refund" in q:
            return "You can return any unworn item within thirty days for a full refund or exchange using our prepaid eco-mailer."

        # Default fallback
        return "Thanks for asking about that. We are happy to help you explore our sustainable collection. Is there a specific item you are looking for?"

    @property
    def _llm_type(self) -> str:
        return "mock-kiosk-chat-model"


def get_llm() -> BaseChatModel:
    """Instantiate the configured LangChain chat model.
    
    Uses ChatAnthropic if an API key is available and mock mode is not forced;
    otherwise falls back to MockKioskChatModel.
    """
    if not settings.is_mock_enabled and settings.anthropic_api_key:
        try:
            from langchain_anthropic import ChatAnthropic
            # ChatAnthropic: Modern LangChain wrapper for Anthropic's Claude models.
            return ChatAnthropic(
                model=settings.model_name,
                anthropic_api_key=settings.anthropic_api_key,
                temperature=0.3,
            )
        except Exception:
            return MockKioskChatModel()
    return MockKioskChatModel()


def create_kiosk_graph():
    """Create the conversational LangGraph state machine.
    
    Uses MessagesState to store conversation history and session_manager.checkpointer
    for thread-based persistence keyed by session_id.
    """
    llm = get_llm()

    def call_model(state: MessagesState) -> Dict[str, List[BaseMessage]]:
        # Prepend system prompt to the messages sent to the model
        messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
        response = llm.invoke(messages)
        return {"messages": [response]}

    workflow = StateGraph(state_schema=MessagesState)
    workflow.add_node("model", call_model)
    workflow.add_edge(START, "model")
    workflow.add_edge("model", END)

    # Compile the graph with LangGraph's native MemorySaver checkpointer
    app = workflow.compile(checkpointer=session_manager.checkpointer)
    return app


# Singleton compiled graph
kiosk_brain = create_kiosk_graph()


def ask_avatar(message: str, session_id: str) -> str:
    """Convenience helper to send a message to the avatar and receive a spoken reply."""
    # Touch session to handle activity timestamp and idle timeout
    session_manager.touch(session_id)

    config = {"configurable": {"thread_id": session_id}}
    result = kiosk_brain.invoke(
        {"messages": [HumanMessage(content=message)]},
        config=config,
    )
    last_msg = result["messages"][-1].content
    return clean_spoken_text(str(last_msg))
