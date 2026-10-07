import json, os
from fastapi import FastAPI
from pydantic import BaseModel
from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
import catalog

SYSTEM = """You are Greenie, the friendly shopping assistant for an eco-friendly store that sells
lifestyle essentials handcrafted from 100% upcycled rice-husk biocomposite.
Be warm, polite, natural and concise. Prices are in INR (use the rupee symbol).
Rules:
- NEVER state a price, discount, stock status, product detail or policy from memory. Always call a tool first.
- If a tool returns nothing, say honestly that you don't have that information and suggest contacting the team.
- Do not pressure the customer. If the question is unrelated to the store, politely steer back.
- Reply in the customer's language (English or Hinglish)."""


@tool
def search_products(query: str = "", category: str = "", max_price: float = 0) -> str:
    """Search products by keyword, category (Drinkware, Kitchen & Dining, Desk & Office, Corporate Gifts) and/or max price in INR. Use 0 or empty for no filter."""
    res = catalog.search_products(query, category or None, max_price or None)
    return json.dumps(res, ensure_ascii=False) if res else "No matching products found."


@tool
def get_product(name: str) -> str:
    """Get full details, price, discount and stock for one product by name."""
    p = catalog.get_product(name)
    return json.dumps(p, ensure_ascii=False) if p else "Product not found."


@tool
def list_categories() -> str:
    """List all product categories in the store."""
    return json.dumps(catalog.list_categories())


@tool
def get_policy(topic: str) -> str:
    """Get a store policy. Topics: shipping, returns, care, bulk_orders."""
    return catalog.get_policy(topic) or "No policy information available."


model = init_chat_model(os.getenv("LLM_MODEL", "anthropic:claude-sonnet-4-6"), temperature=0.4)
agent = create_agent(
    model,
    tools=[search_products, get_product, list_categories, get_policy],
    system_prompt=SYSTEM,
    checkpointer=InMemorySaver(),
)

api = FastAPI()


class Chat(BaseModel):
    session_id: str
    message: str


def _text(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


@api.post("/chat")
def chat(body: Chat):
    result = agent.invoke(
        {"messages": [{"role": "user", "content": body.message}]},
        config={"configurable": {"thread_id": body.session_id}},
    )
    return {"reply": _text(result["messages"][-1].content)}
