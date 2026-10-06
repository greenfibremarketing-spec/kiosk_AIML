"""Product catalog tools for Green Fibre AI Kiosk Agent.

Provides verified product search, details, stock check, and gift bundle recommendations.
The agent must only state prices and stock numbers obtained directly through these tools.
"""

import json
import os
from typing import Any, Dict, List, Optional
from langchain_core.tools import tool

from app.config import settings

_products_data_cache: Optional[Dict[str, Any]] = None


def _load_catalog() -> Dict[str, Any]:
    """Load and cache products and bundles from products.json."""
    global _products_data_cache
    if _products_data_cache is None:
        file_path = settings.products_file
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Products file not found at: {file_path}")
        with open(file_path, "r", encoding="utf-8") as f:
            _products_data_cache = json.load(f)
    return _products_data_cache


@tool
def search_products(query: str) -> str:
    """Search for Green Fibre products by name, category, material, or keyword.
    
    Args:
        query: Search term (e.g., 'linen pants', 'cotton tee', 'hoodie', 'socks', 'towel', 'soap').
        
    Returns:
        JSON string list of matching products with id, name, category, price, and brief description.
    """
    catalog = _load_catalog()
    products = catalog.get("products", [])
    q = query.lower().strip()

    matches = []
    # If customer is explicitly searching for synthetic / petroleum fabrics, Green Fibre does not make them
    synthetic_keywords = ["nylon", "polyester", "acrylic", "fleece polyester", "synthetic jacket", "synthetic shirt"]
    if any(sk in q for sk in synthetic_keywords):
        return f"No products found matching '{query}'. Green Fibre never produces synthetic or plastic garments."

    for p in products:
        name_cat = f"{p.get('name', '')} {p.get('category', '')}".lower()
        desc = p.get('description', '').lower()

        # Score name/category match higher
        if q in name_cat or all(word in name_cat for word in q.split() if len(word) > 2):
            matches.append(p)
        elif q in desc or any(word in name_cat for word in q.split() if len(word) > 3):
            matches.append(p)

    if not matches:
        return f"No products found matching '{query}'. State honestly that we do not have this item in our catalog."

    clean_results = [
        {
            "id": p["id"],
            "name": p["name"],
            "category": p["category"],
            "price": p["price"],
            "description": p["description"],
        }
        for p in matches[:4]
    ]
    return json.dumps(clean_results, indent=2)


@tool
def get_product_details(product_id: str) -> str:
    """Retrieve complete verified details for a specific product by its ID.
    
    Args:
        product_id: The unique product identifier (e.g., 'GF-TEE-01', 'GF-HOOD-02').
        
    Returns:
        JSON string with name, price, stock, description, and sustainability notes.
    """
    catalog = _load_catalog()
    products = catalog.get("products", [])
    pid = product_id.upper().strip()

    for p in products:
        if p["id"].upper() == pid:
            return json.dumps(p, indent=2)

    return f"Product with ID '{product_id}' was not found in the catalog. State honestly that it does not exist."


@tool
def check_stock(product_id: str) -> str:
    """Check verified inventory and available stock for a specific product ID.
    
    Args:
        product_id: The unique product identifier (e.g., 'GF-TEE-01', 'GF-PANT-03').
        
    Returns:
        Stock level and availability message. Never guess or state stock without calling this tool.
    """
    catalog = _load_catalog()
    products = catalog.get("products", [])
    pid = product_id.upper().strip()

    for p in products:
        if p["id"].upper() == pid:
            stock = p.get("stock", 0)
            if stock > 0:
                return f"Product {p['name']} ({p['id']}) has {stock} units currently in stock."
            else:
                return f"Product {p['name']} ({p['id']}) is currently out of stock."

    return f"Product with ID '{product_id}' was not found in inventory."


@tool
def get_gift_bundles(budget: Optional[float] = None) -> str:
    """Find curated eco-friendly gift bundles, optionally filtered by maximum budget in USD.
    
    Args:
        budget: Optional maximum budget amount in dollars (e.g., 50.0, 100.0, 200.0).
        
    Returns:
        JSON string list of suitable gift bundles with pricing, included items, and savings.
    """
    catalog = _load_catalog()
    bundles = catalog.get("gift_bundles", [])

    results = []
    for b in bundles:
        if budget is None or b.get("price", 0) <= budget:
            results.append({
                "id": b["id"],
                "name": b["name"],
                "price": b["price"],
                "regular_value": b.get("regular_value"),
                "items": b["items"],
                "description": b["description"],
            })

    if not results:
        if budget is not None:
            return f"No gift bundles found within budget of ${budget:.2f}. The lowest price bundle is $45.00."
        return "No gift bundles currently available."

    return json.dumps(results, indent=2)


# Export tool list
ALL_TOOLS = [search_products, get_product_details, check_stock, get_gift_bundles]
