"""LangChain & LangGraph brain for the Green Fibre AI Avatar Kiosk.

Constructs the conversational engine using modern LangGraph StateGraph,
selectable LLM factory (Groq, Anthropic, Mock), tool calling (search, details, stock, bundles),
RAG retrieval context, rate-limit retry with capped exponential backoff (max 3s),
recursion limits, post-generation factual number validator, and thread-based memory checkpointing.
"""

import json
import logging
import re
import time
import uuid
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple, Union

# LangChain Core imports
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool

# LangGraph imports
from langgraph.errors import GraphRecursionError
from langgraph.graph import StateGraph, START, END, MessagesState
from langgraph.prebuilt import ToolNode, tools_condition

from app.callbacks import TraceCallbackHandler
from app.config import settings
from app.rag import retrieve_relevant_chunks, search_knowledge, warmup_rag
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

FALLBACK_VALIDATION_REPLY = (
    "I'd be glad to help with that, but I want to ensure I provide exact verified details. "
    "Please check with a kiosk store associate for verified price and availability."
)

FALLBACK_RECURSION_REPLY = (
    "I was unable to complete looking up all details for that request. "
    "Please ask a store associate at the kiosk for assistance."
)

WORD_TO_NUM: Dict[str, float] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "eighteen": 18, "twenty": 20,
    "twenty-two": 22, "twenty-four": 24, "twenty-eight": 28, "thirty": 30,
    "thirty-two": 32, "thirty-four": 34, "thirty-five": 35, "thirty-eight": 38,
    "forty": 40, "forty-two": 42, "forty-five": 45, "forty-eight": 48,
    "fifty": 50, "sixty": 60, "sixty-eight": 68, "seventy-four": 74,
    "seventy-five": 75, "eighty": 80, "eighty-eight": 88, "ninety-five": 95,
    "one hundred": 100, "one hundred sixty": 160, "one hundred eighty-five": 185,
    "two hundred thirty": 230,
}


def clean_spoken_text(text: str) -> str:
    """Sanitize model output to ensure plain spoken sentences for TTS.
    
    Removes markdown markers, asterisks, hash headers, bullet dashes, and emojis.
    """
    if not text:
        return ""
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[\*_]{1,3}(.*?)[*_]{1,3}", r"\1", text)
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.MULTILINE)
    text = text.replace("`", "")
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002702-\U000027B0"
        "\U000024C2-\U0001F251"
        "]+",
        flags=re.UNICODE,
    )
    text = emoji_pattern.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_numbers_from_text(text: str) -> Set[float]:
    """Extract all numeric quantities (digits and written numbers) from text."""
    numbers: Set[float] = set()
    for match in re.findall(r"\b\d+(?:\.\d+)?\b", text):
        try:
            numbers.add(float(match))
        except ValueError:
            pass

    text_lower = text.lower()
    # Sort compound words first (e.g. eighty-eight before eight)
    sorted_words = sorted(WORD_TO_NUM.keys(), key=len, reverse=True)
    for word in sorted_words:
        pattern = r"(?<![\w-])" + re.escape(word) + r"(?![\w-])"
        if re.search(pattern, text_lower):
            numbers.add(float(WORD_TO_NUM[word]))

    return numbers


def validate_reply_factual_numbers(
    reply: str,
    tool_outputs: List[str],
    rag_chunks: List[Dict[str, Any]],
) -> Tuple[bool, Optional[str]]:
    """Validate that any specific price or stock number in reply originates from tools or RAG.
    
    Returns (is_valid, reason).
    """
    allowed_numbers: Set[float] = set()

    for out in tool_outputs:
        allowed_numbers.update(extract_numbers_from_text(out))

    for chunk in rag_chunks:
        allowed_numbers.update(extract_numbers_from_text(chunk.get("content", "")))

    # Generic integers allowed without grounding (e.g. 1 to 3 sentences, 1 or 2 options)
    whitelisted_generic = {1.0, 2.0, 3.0}

    # Extract price numbers from candidate reply
    price_patterns = re.findall(r"\$\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:dollars|cents|usd|bucks)", reply, flags=re.IGNORECASE)
    reply_prices: Set[float] = set()
    for p1, p2 in price_patterns:
        val_str = p1 or p2
        if val_str:
            try:
                reply_prices.add(float(val_str))
            except ValueError:
                pass

    # Extract word prices (e.g. "eighty-eight dollars"), sorting compound words first
    sorted_words = sorted(WORD_TO_NUM.keys(), key=len, reverse=True)
    for word in sorted_words:
        pattern = r"(?<![\w-])" + re.escape(word) + r"\s+(?:dollars|cents|usd)\b"
        if re.search(pattern, reply, flags=re.IGNORECASE):
            reply_prices.add(float(WORD_TO_NUM[word]))

    # Check unverified prices
    for p in reply_prices:
        if p not in allowed_numbers and p not in whitelisted_generic:
            return False, f"Price {p} in reply was not found in tool outputs or verified RAG context."

    # Extract stock specific numbers (e.g., "45 units", "22 in stock")
    stock_patterns = re.findall(r"(\d+)\s*(?:units|in stock|available|items left|pieces)", reply, flags=re.IGNORECASE)
    for s in stock_patterns:
        try:
            s_val = float(s)
            if s_val not in allowed_numbers and s_val not in whitelisted_generic:
                return False, f"Stock quantity {s_val} in reply was not found in verified tool outputs."
        except ValueError:
            pass

    return True, None


class MockKioskChatModel(BaseChatModel):
    """Realistic offline fallback chat model for kiosk development and testing."""

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
        last_msg = messages[-1]
        if isinstance(last_msg, ToolMessage):
            spoken_reply = self._synthesize_tool_reply(last_msg.content)
            generation = ChatGeneration(message=AIMessage(content=spoken_reply))
            return ChatResult(generations=[generation])

        last_human_query = ""
        for m in reversed(messages):
            if isinstance(m, HumanMessage):
                last_human_query = str(m.content).strip()
                break

        tool_call = self._decide_tool_call(last_human_query)
        if tool_call:
            generation = ChatGeneration(message=AIMessage(content="", tool_calls=[tool_call]))
            return ChatResult(generations=[generation])

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
        if "not carried" in tool_output.lower():
            return "I checked our store catalog and we do not carry that item. We specialize exclusively in sustainable plant-based textiles."

        if "out of stock" in tool_output.lower():
            return tool_output

        if "in stock with" in tool_output.lower():
            return tool_output

        try:
            data = json.loads(tool_output)
            if isinstance(data, list) and data:
                item = data[0]
                if "regular_value" in item:
                    return (
                        f"We offer the {item['name']} for {int(item['price'])} dollars, "
                        f"which includes {len(item.get('items', []))} curated eco essentials."
                    )
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

        if any(w in q for w in ["python", "code", "weather", "president", "math", "bitcoin", "scrape"]):
            return "I can only assist with Green Fibre products and store policies."

        if any(w in q for w in ["hi", "hello", "hey", "good morning", "good afternoon"]):
            return "Hello! Welcome to Green Fibre. How can I help you discover our eco-friendly collection today?"

        if "return" in q or "refund" in q:
            return "You can return any unworn, unwashed item within thirty days for a full refund. Return shipping is completely free with a digital QR code."

        if "shipping" in q or "delivery" in q or "how long" in q:
            return "Standard carbon-neutral shipping takes three to five business days and is free on orders over fifty dollars. Express delivery takes one to two business days for nine dollars and ninety-nine cents."

        if "wash" in q or "care" in q or "clean" in q:
            return "We recommend washing in cold water at thirty degrees Celsius on a gentle cycle with plant-based detergent, then line drying to preserve the fibers."

        if "material" in q or "cotton" in q or "linen" in q or "hemp" in q or "bamboo" in q:
            return "We craft all pieces exclusively from GOTS-certified organic cotton, European flax linen, Himalayan organic hemp, and closed-loop bamboo lyocell."

        if "seed" in q or "tag" in q or "plant" in q:
            return "Every price tag is embedded with non-GMO wildflower and thyme seeds that you can plant directly in soil to grow garden blooms."

        if "brand" in q or "who are you" in q or "what brand" in q:
            return "Green Fibre is an eco-friendly brand specializing in apparel and home essentials made from pure, sustainable plant fibers."

        return "I don't have that specific information in my store records. May I help you with another question about our products or policies?"

    @property
    def _llm_type(self) -> str:
        return "mock-kiosk-chat-model"


def get_llm() -> BaseChatModel:
    """Factory creating the configured LLM provider."""
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
            temperature=0.2,
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
            temperature=0.2,
        )

    raise ValueError(
        f"Unsupported LLM_PROVIDER '{settings.llm_provider}'. "
        "Supported providers are: 'groq', 'anthropic', 'mock'."
    )


def invoke_llm_with_retry(
    llm: Runnable,
    messages: List[BaseMessage],
    max_retries: int = 3,
    max_total_wait: Optional[float] = None,
    **kwargs: Any,
) -> BaseMessage:
    """Invoke LLM with capped backoff (<= 3s total) respecting retry-after headers."""
    cap_wait = (
        max_total_wait if max_total_wait is not None else settings.max_retry_wait_seconds
    )
    total_waited = 0.0
    backoff = 0.8

    for attempt in range(max_retries + 1):
        try:
            return llm.invoke(messages, **kwargs)
        except Exception as e:
            err_str = str(e).lower()
            is_rate_limit = "429" in err_str or "rate_limit" in err_str or "rate limit" in err_str

            if is_rate_limit and attempt < max_retries:
                retry_after: Optional[float] = None
                if hasattr(e, "response") and hasattr(e.response, "headers"):
                    ra_hdr = e.response.headers.get("retry-after")
                    if ra_hdr and ra_hdr.isdigit():
                        retry_after = float(ra_hdr)

                target_sleep = retry_after if retry_after is not None else backoff
                remaining = max(0.0, cap_wait - total_waited)

                if remaining <= 0.1:
                    logger.error("Rate limit retry budget of %.1fs exhausted: %s", cap_wait, e)
                    return AIMessage(content=FALLBACK_RATE_LIMIT_REPLY)

                actual_sleep = min(target_sleep, remaining)
                logger.warning(
                    "Rate limit 429 encountered (attempt %d/%d). Sleeping %.2fs (budget remaining: %.2fs)...",
                    attempt + 1, max_retries, actual_sleep, remaining
                )
                time.sleep(actual_sleep)
                total_waited += actual_sleep
                backoff *= 1.5
            else:
                if is_rate_limit:
                    return AIMessage(content=FALLBACK_RATE_LIMIT_REPLY)
                raise e

    return AIMessage(content=FALLBACK_RATE_LIMIT_REPLY)


def create_kiosk_graph():
    """Create the conversational LangGraph agent with tool calling, RAG, and memory."""
    llm = get_llm()
    model_with_tools = llm.bind_tools(ALL_TOOLS)

    def agent_node(state: MessagesState, config: Optional[RunnableConfig] = None) -> Dict[str, List[BaseMessage]]:
        user_query = ""
        for m in reversed(state["messages"]):
            if isinstance(m, HumanMessage):
                user_query = str(m.content)
                break

        rag_context = ""
        retrieved_chunks = []
        if user_query:
            try:
                retrieved_chunks = retrieve_relevant_chunks(
                    user_query,
                    k=2,
                    score_threshold=settings.rag_score_threshold,
                )
                if retrieved_chunks:
                    context_lines = [f"[{c['source']}]: {c['content']}" for c in retrieved_chunks]
                    rag_context = "\n\n".join(context_lines)
            except Exception as e:
                logger.debug("RAG retrieval skipped or failed: %s", e)

        if config and "callbacks" in config:
            cbs = config["callbacks"]
            handler_list = getattr(cbs, "handlers", cbs if isinstance(cbs, (list, tuple)) else [cbs] if cbs else [])
            for cb in handler_list:
                if isinstance(cb, TraceCallbackHandler):
                    cb.record_retrieved_chunks(retrieved_chunks)

        system_content = SYSTEM_PROMPT
        if rag_context:
            system_content += (
                f"\n\nVERIFIED KNOWLEDGE BASE CONTEXT:\n{rag_context}\n\n"
                "Use the above verified facts to formulate your spoken answer. "
                "Keep it to 1 to 3 spoken sentences without markdown."
            )

        messages = [SystemMessage(content=system_content)] + state["messages"]
        response = invoke_llm_with_retry(model_with_tools, messages)
        return {"messages": [response]}

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
    """Send a message to the avatar with recursion limits, post-generation validation, and fallback."""
    session_manager.touch(session_id)

    config: Dict[str, Any] = {
        "configurable": {"thread_id": session_id},
        "recursion_limit": settings.agent_recursion_limit,
    }
    if trace_handler:
        trace_handler.start_turn()
        config["callbacks"] = [trace_handler]

    try:
        try:
            result = kiosk_brain.invoke(
                {"messages": [HumanMessage(content=message)]},
                config=config,
            )
        except GraphRecursionError:
            logger.warning("Recursion limit of %d hit for session %s", settings.agent_recursion_limit, session_id)
            return FALLBACK_RECURSION_REPLY

        # Collect tool outputs generated in this turn
        turn_tool_outputs = []
        for msg in result.get("messages", []):
            if isinstance(msg, ToolMessage):
                turn_tool_outputs.append(str(msg.content))

        # Retrieve RAG chunks for validation context
        rag_chunks = []
        if trace_handler:
            rag_chunks = trace_handler.retrieved_chunks

        raw_reply = str(result["messages"][-1].content)
        cleaned_reply = clean_spoken_text(raw_reply)

        # Post-generation factual number validator
        is_valid, reason = validate_reply_factual_numbers(
            cleaned_reply,
            turn_tool_outputs,
            rag_chunks,
        )

        if not is_valid:
            logger.warning("Post-generation validation failed: %s. Regenerating once...", reason)
            try:
                # Fast direct regeneration with strict correction prompt
                llm_raw = get_llm()
                tool_context = "\n".join(turn_tool_outputs)
                correction_prompt = [
                    SystemMessage(
                        content=SYSTEM_PROMPT + f"\n\nVERIFIED TOOL DATA:\n{tool_context}\n\n"
                        "CRITICAL: State ONLY numbers that appear in the verified tool data above."
                    ),
                    HumanMessage(content=message),
                ]
                regen_resp = llm_raw.invoke(correction_prompt)
                regen_reply = clean_spoken_text(str(regen_resp.content))
                is_valid_regen, _ = validate_reply_factual_numbers(
                    regen_reply,
                    turn_tool_outputs,
                    rag_chunks,
                )
                if is_valid_regen:
                    return regen_reply
            except Exception as e:
                logger.debug("Regeneration error: %s", e)

            return FALLBACK_VALIDATION_REPLY

        return cleaned_reply

    finally:
        if trace_handler:
            trace_handler.end_turn()
