"""Data layer. The bot only reads data through these functions.
To go live, replace _load() with a SQL query or a REST call to your store backend.
Nothing else in the project needs to change."""
import json, time
from pathlib import Path

DATA = Path(__file__).parent / "data"
TTL_SECONDS = 30
_cache = {}


def _load(name):
    hit = _cache.get(name)
    if hit and time.time() - hit[0] < TTL_SECONDS:
        return hit[1]
    data = json.loads((DATA / f"{name}.json").read_text(encoding="utf-8"))
    _cache[name] = (time.time(), data)
    return data


def _with_discount(p):
    p = dict(p)
    p["discount_percent"] = round((p["mrp"] - p["price"]) * 100 / p["mrp"]) if p.get("mrp") else 0
    return p


def search_products(query="", category=None, max_price=None):
    q = query.lower().strip()
    out = []
    for p in _load("products"):
        text = f'{p["name"]} {p["category"]} {p["description"]}'.lower()
        if q and not any(w in text for w in q.split()):
            continue
        if category and p["category"].lower() != category.lower():
            continue
        if max_price is not None and p["price"] > max_price:
            continue
        out.append(_with_discount(p))
    return out


def get_product(name):
    n = name.lower().strip()
    for p in _load("products"):
        if n in p["name"].lower() or n == p["id"]:
            return _with_discount(p)
    return None


def list_categories():
    return sorted({p["category"] for p in _load("products")})


def get_policy(topic):
    return _load("policies").get(topic.lower().strip())
