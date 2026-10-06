"""LangChain & LangGraph brain for the Green Fibre AI Avatar Kiosk.

Constructs the conversational engine using modern LangGraph StateGraph,
selectable LLM factory (Groq, Anthropic, Mock), tool calling (search, details, stock, bundles),
RAG retrieval context, rate-limit retry with exponential backoff, and thread-based memory checkpointing.
"""

import json
import logging
import re
import time
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Union

# LangChain Core imports
# BaseChatModel: Standard abstract base class for all LangChain chat models.
from langchain_core.language_models.chat_models import BaseChatModel
# Message types: Standard message representations in conversational graph workflows.
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
# Output containers
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool

# LangGraph imports
# StateGraph, MessagesState, ToolNode, tools_condition: Modern orchestration for agents with tool calling.
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.prebuilt import ToolNode, tools_condition

from app.callbacks import TraceCallbackHandler
from app.config import settings
from app.rag import retrieve_relevant_chunks
from app.sessions import session_manager
from app.tools import ALL_TOOLS

logger = logging.getLogger("green_fibre.brain")

# Spoken avatar guardrails in system prompt
SYSTEM_PROMPT = """You are the friendly, voice-first AI avatar assistant at the Green Fibre kiosk.
Green Fibre is an eco-friendly e-commerce brand specializing in sustainable apparel and home goods made from organic cotton, linen, hemp, and bamboo.

STRICT VOICE & SPOKEN RULES:
1. Your responses will be read out loud by a text-to-speech engine. Speak in natural, warm, and concise spoken English.
2. Keep your replies short: 1 to 3 sentences maximum.
3. NEVER use markdown formatting: no asterisks, no bolding, no headings, no code blocks, no backticks.
4. NEVER use bullet points or numbered lists. Use flowing sentences instead.
5. NEVER use emojis.

TOOL USAGE & ACCURACY GUARDRAILS:
1. You have access to tools for searching products, checking product details, checking stock, and finding gift bundles.
2. CRITICAL: You must NEVER state or invent a product price, discount, or stock number unless it was returned by a tool call in the current conversation.
3. When recommending or describing items, state the exact price and stock from the tool output.
4. If a tool returns no products or states that an item is not in the catalog, state honestly and politely that Green Fibre does not carry it.
5. Never ask for or store sensitive personal information such as passwords or credit cards.
6. For non-product topics (shipping, returns, materials, store hours), use the verified knowledge base context. If information is not available, honestly say you do not know.
"""

FALLBACK_RATE_LIMIT_REPLY = (
    "I am currently experiencing high visitor traffic at the kiosk. "
    "Please give me just a moment and ask again, or consult a store associate."
)


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
    
    Emulates tool calling and RAG grounding without requiring external API credentials.
    """

    def bind_tools(
        self,
        tools: Sequence[Union[Dict[str, Any], type, Callable, BaseTool]],
        **kwargs: Any,
    ) -> Runnable[Any, BaseMessage]:
        return self

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[Any] = None,
        **kwargs: Any,
    ) -> ChatResult:
        # Check if the last message was a ToolMessage
        last_msg = messages[-1]
        if isinstance(last_msg, ToolMessage):
            # Synthesize final spoken answer from verified tool data
            spoken_reply = self._synthesize_tool_reply(last_msg.content)
            generation = ChatGeneration(message=AIMessage(content=spoken_reply))
            return ChatResult(generations=[generation])

        # Find the latest human message
        last_human_query = ""
        for m in reversed(messages):
            if isinstance(m, HumanMessage):
                last_human_query = str(m.content).strip()
                break

        # Check if query needs a tool call
        tool_call = self._decide_tool_call(last_human_query)
        if tool_call:
            generation = ChatGeneration(message=AIMessage(content="", tool_calls=[tool_call]))
            return ChatResult(generations=[generation])

        # Otherwise answer conversational / RAG policy questions
        text = self._generate_rag_reply(last_human_query)
        generation = ChatGeneration(message=AIMessage(content=text))
        return ChatResult(generations=[generation])

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[Any] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        result = self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        ai_msg = result.generations[0].message
        if isinstance(ai_msg, AIMessage) and ai_msg.tool_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_calls=ai_msg.tool_calls))
        else:
            full_text = str(ai_msg.content)
            words = full_text.split(" ")
            for i, word in enumerate(words):
                chunk_text = word if i == len(words) - 1 else word + " "
                chunk = ChatGenerationChunk(message=AIMessageChunk(content=chunk_text))
                if run_manager:
                    run_manager.on_llm_new_token(chunk_text)
                yield chunk

    def _decide_tool_call(self, query: str) -> Optional[Dict[str, Any]]:
        q = query.lower()

        # Check stock inquiry
        if any(w in q for w in ["stock", "in stock", "how many", "quantity", "inventory"]):
            for pid in ["gf-tee-01", "gf-hood-02", "gf-pant-03", "gf-sock-04", "gf-towel-05", "gf-bed-06", "gf-tote-07"]:
                if pid in q:
                    return {
                        "name": "check_stock",
                        "args": {"product_id": pid.upper()},
                        "id": f"call_{uuid.uuid4().hex[:6]}",
                        "type": "tool_call",
                    }
            if "hoodie" in q:
                return {"name": "check_stock", "args": {"product_id": "GF-HOOD-02"}, "id": f"call_{uuid.uuid4().hex[:6]}", "type": "tool_call"}
            if "tee" in q or "t-shirt" in q:
                return {"name": "check_stock", "args": {"product_id": "GF-TEE-01"}, "id": f"call_{uuid.uuid4().hex[:6]}", "type": "tool_call"}

        # Gift bundles inquiry
        if any(w in q for w in ["bundle", "gift", "package", "budget"]):
            budget = None
            numbers = re.findall(r"\d+", q)
            if numbers:
                budget = float(numbers[0])
            return {
                "name": "get_gift_bundles",
                "args": {"budget": budget},
                "id": f"call_{uuid.uuid4().hex[:6]}",
                "type": "tool_call",
            }

        # Product search inquiry
        product_keywords = ["hoodie", "tee", "shirt", "pant", "sock", "towel", "duvet", "tote", "apron", "soap", "robe", "jacket", "leather", "nylon", "polyester", "price", "how much", "cost", "buy"]
        if any(w in q for w in product_keywords):
            search_term = q.replace("do you have", "").replace("what is the price of", "").replace("how much is", "").replace("tell me about", "").strip()
            return {
                "name": "search_products",
                "args": {"query": search_term or q},
                "id": f"call_{uuid.uuid4().hex[:6]}",
                "type": "tool_call",
            }

        return None

    def _synthesize_tool_reply(self, tool_output: str) -> str:
        # Check for empty / not found
        if "No products found" in tool_output or "not found" in tool_output:
            return "I checked our store catalog and we do not have that item in stock. We only offer verified organic and plant-based goods."

        if "currently in stock" in tool_output or "out of stock" in tool_output:
            return tool_output

        try:
            data = json.loads(tool_output)
            if isinstance(data, list) and data:
                item = data[0]
                if "regular_value" in item:
                    # Gift bundle
                    return (
                        f"We offer the {item['name']} for {int(item['price'])} dollars, "
                        f"which includes {len(item.get('items', []))} curated eco essentials."
                    )
                # Single product
                return (
                    f"Our {item['name']} is available for {int(item['price'])} dollars. "
                    f"{item.get('description', '')}"
                )
            if isinstance(data, dict):
                return (
                    f"Our {data.get('name', 'product')} is priced at {int(data.get('price', 0))} dollars, "
                    f"with {data.get('stock', 0)} units currently available."
                )
        except Exception:
            pass

        return clean_spoken_text(tool_output)

    def _generate_rag_reply(self, query: str) -> str:
        q = query.lower()

        # Off-topic guardrail checks
        if any(w in q for w in ["python", "code", "weather", "president", "math", "bitcoin"]):
            return "I can only assist with Green Fibre products and store policies."

        # Greetings
        if any(w in q for w in ["hi", "hello", "hey", "good morning", "good afternoon"]):
            return "Hello! Welcome to Green Fibre. How can I help you discover our eco-friendly collection today?"

        # Returns and refunds (grounded in shipping_returns.md)
        if "return" in q or "refund" in q:
            return "You can return any unworn, unwashed item within thirty days for a full refund. Return shipping is completely free with a digital QR code."

        # Shipping and delivery (grounded in shipping_returns.md)
        if "shipping" in q or "delivery" in q or "how long" in q:
            return "Standard carbon-neutral shipping takes three to five business days and is free on orders over fifty dollars. Express delivery takes one to two business days for nine dollars and ninety-nine cents."

        # Garment care / washing (grounded in faq.md)
        if "wash" in q or "care" in q or "clean" in q:
            return "We recommend washing in cold water at thirty degrees Celsius on a gentle cycle with plant-based detergent, then line drying to preserve the fibers."

        # Materials / Fabrics (grounded in brand_story.md)
        if "material" in q or "cotton" in q or "linen" in q or "hemp" in q or "bamboo" in q:
            return "We craft all pieces exclusively from GOTS-certified organic cotton, European flax linen, Himalayan organic hemp, and closed-loop bamboo lyocell."

        # Hangtags / Seeds (grounded in brand_story.md)
        if "seed" in q or "tag" in q or "plant" in q:
            return "Every price tag is embedded with non-GMO wildflower and thyme seeds that you can plant directly in soil to grow garden blooms."

        # Default honest fallback
        return "I don't have that specific information in my store records. May I help you with another question about our products or policies?"

    @property
    def _llm_type(self) -> str:
        return "mock-kiosk-chat-model"


def get_llm() -> BaseChatModel:
    """Factory creating the configured LLM provider.
    
    Supports:
      - 'groq': ChatGroq via langchain-groq
      - 'anthropic': ChatAnthropic via langchain-anthropic
      - 'mock': MockKioskChatModel
    Raises a clear ValueError if a required API key is missing. No silent fallbacks.
    """
    provider = settings.llm_provider.lower().strip()

    if provider == "mock":
        logger.warning("=" * 70)
        logger.warning("[WARNING] LLM_PROVIDER is set to 'mock'. Using MockKioskChatModel.")
        logger.warning("          Responses are simulated for local offline development.")
        logger.warning("=" * 70)
        return MockKioskChatModel()

    if provider == "groq":
        if not settings.groq_api_key or "your_" in settings.groq_api_key.lower():
            raise ValueError(
                "LLM_PROVIDER is set to 'groq', but GROQ_API_KEY is missing or contains a placeholder in .env. "
                "Please configure a valid GROQ_API_KEY in .env, or set LLM_PROVIDER=mock for offline testing."
            )
        from langchain_groq import ChatGroq
        logger.info("Initializing Groq provider with model: %s", settings.groq_model_name)
        return ChatGroq(
            model_name=settings.groq_model_name,
            api_key=settings.groq_api_key,
            temperature=0.3,
        )

    if provider == "anthropic":
        if not settings.anthropic_api_key or "your_" in settings.anthropic_api_key.lower():
            raise ValueError(
                "LLM_PROVIDER is set to 'anthropic', but ANTHROPIC_API_KEY is missing or contains a placeholder in .env. "
                "Please configure a valid ANTHROPIC_API_KEY in .env, or set LLM_PROVIDER=mock for offline testing."
            )
        from langchain_anthropic import ChatAnthropic
        logger.info("Initializing Anthropic provider with model: %s", settings.model_name)
        return ChatAnthropic(
            model=settings.model_name,
            anthropic_api_key=settings.anthropic_api_key,
            temperature=0.3,
        )

    raise ValueError(
        f"Unsupported LLM_PROVIDER '{settings.llm_provider}'. "
        "Supported providers are: 'groq', 'anthropic', 'mock'."
    )


def invoke_llm_with_retry(
    llm: Runnable,
    messages: List[BaseMessage],
    max_retries: int = 3,
    initial_backoff: float = 1.0,
    **kwargs: Any,
) -> BaseMessage:
    """Invoke the LLM with exponential backoff on rate-limit (429) errors.
    
    If retries are exhausted, returns a friendly spoken fallback reply.
    """
    backoff = initial_backoff

    for attempt in range(max_retries + 1):
        try:
            return llm.invoke(messages, **kwargs)
        except Exception as e:
            err_str = str(e).lower()
            is_rate_limit = "429" in err_str or "rate_limit" in err_str or "rate limit" in err_str

            if is_rate_limit and attempt < max_retries:
                logger.warning(
                    "Rate limit encountered (attempt %d/%d). Retrying in %.1fs...",
                    attempt + 1, max_retries, backoff
                )
                time.sleep(backoff)
                backoff *= 2.0
            else:
                if is_rate_limit:
                    logger.error("Rate limit retries exhausted: %s", e)
                    return AIMessage(content=FALLBACK_RATE_LIMIT_REPLY)
                raise e

    return AIMessage(content=FALLBACK_RATE_LIMIT_REPLY)


def create_kiosk_graph():
    """Create the conversational LangGraph agent with tool calling, RAG, and memory."""
    llm = get_llm()
    # Bind product tools to the model
    model_with_tools = llm.bind_tools(ALL_TOOLS)

    def agent_node(state: MessagesState, config: Optional[RunnableConfig] = None) -> Dict[str, List[BaseMessage]]:
        # Extract user query
        user_query = ""
        for m in reversed(state["messages"]):
            if isinstance(m, HumanMessage):
                user_query = str(m.content)
                break

        # Retrieve relevant verified knowledge chunks
        rag_context = ""
        retrieved_chunks = []
        if user_query:
            try:
                retrieved_chunks = retrieve_relevant_chunks(user_query, k=2)
                if retrieved_chunks:
                    context_lines = [f"[{c['source']}]: {c['content']}" for c in retrieved_chunks]
                    rag_context = "\n\n".join(context_lines)
            except Exception as e:
                logger.debug("RAG retrieval skipped or failed: %s", e)

        # Notify trace callback handler if present in config
        if config and "callbacks" in config:
            cbs = config["callbacks"]
            handler_list = getattr(cbs, "handlers", cbs if isinstance(cbs, (list, tuple)) else [cbs] if cbs else [])
            for cb in handler_list:
                if isinstance(cb, TraceCallbackHandler):
                    cb.record_retrieved_chunks(retrieved_chunks)

        # Assemble system prompt with RAG grounding
        system_content = SYSTEM_PROMPT
        if rag_context:
            system_content += (
                f"\n\nVERIFIED KNOWLEDGE BASE CONTEXT:\n{rag_context}\n\n"
                "Use the above verified facts to formulate your spoken answer. "
                "Keep it to 1 to 3 spoken sentences without markdown."
            )

        messages = [SystemMessage(content=system_content)] + state["messages"]

        # Call model with retry wrapper for 429 backoff
        response = invoke_llm_with_retry(model_with_tools, messages)
        return {"messages": [response]}

    # LangGraph StateGraph with ToolNode and conditional edges
    workflow = StateGraph(state_schema=MessagesState)
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", ToolNode(ALL_TOOLS))

    workflow.add_edge(START, "agent")
    workflow.add_conditional_edges("agent", tools_condition)
    workflow.add_edge("tools", "agent")

    app = workflow.compile(checkpointer=session_manager.checkpointer)
    return app


# Singleton compiled graph
kiosk_brain = create_kiosk_graph()


def ask_avatar(
    message: str,
    session_id: str,
    trace_handler: Optional[TraceCallbackHandler] = None,
) -> str:
    """Send a message to the avatar and receive a clean spoken reply, optionally capturing trace."""
    session_manager.touch(session_id)

    config: Dict[str, Any] = {"configurable": {"thread_id": session_id}}
    if trace_handler:
        trace_handler.start_turn()
        config["callbacks"] = [trace_handler]

    try:
        result = kiosk_brain.invoke(
            {"messages": [HumanMessage(content=message)]},
            config=config,
        )
        last_msg = result["messages"][-1].content
        spoken_text = clean_spoken_text(str(last_msg))
    finally:
        if trace_handler:
            trace_handler.end_turn()

    return spoken_text
