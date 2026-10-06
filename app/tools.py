"""Product catalog tools for Green Fibre AI Kiosk Agent.

Provides verified product search, details, stock check, and gift bundle recommendations.
Uses neutral tool return messages and cleanly separates 'not carried' from 'out of stock'.
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
        JSON string list of matching products, or a neutral notice if not carried.
    """
    catalog = _load_catalog()
    products = catalog.get("products", [])
    q = query.lower().strip()

    matches = []
    # Neutral filtering for synthetic queries
    synthetic_keywords = ["nylon", "polyester", "acrylic", "fleece polyester", "synthetic jacket", "synthetic shirt"]
    if any(sk in q for sk in synthetic_keywords):
        return f"No products found matching '{query}'. This item is not carried in the Green Fibre catalog."

    for p in products:
        name_cat = f"{p.get('name', '')} {p.get('category', '')}".lower()
        desc = p.get('description', '').lower()

        if q in name_cat or all(word in name_cat for word in q.split() if len(word) > 2):
            matches.append(p)
        elif q in desc or any(word in name_cat for word in q.split() if len(word) > 3):
            matches.append(p)

    if not matches:
        return f"No products found matching '{query}'. This item is not carried in the Green Fibre catalog."

    clean_results = [
        {
            "id": p["id"],
            "name": p["name"],
            "category": p["category"],
            "price": p["price"],
            "stock": p.get("stock", 0),
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

    return f"Product '{product_id}' is not carried in the catalog."


@tool
def check_stock(product_id: str) -> str:
    """Check inventory availability and stock level for a product ID.
    
    Args:
        product_id: The unique product identifier (e.g., 'GF-TEE-01', 'GF-PANT-03').
        
    Returns:
        Neutral status string distinguishing in-stock, out of stock, or not carried.
    """
    catalog = _load_catalog()
    products = catalog.get("products", [])
    pid = product_id.upper().strip()

    for p in products:
        if p["id"].upper() == pid:
            stock = p.get("stock", 0)
            name = p.get("name", "Product")
            price = p.get("price", 0.0)
            if stock > 0:
                return f"Product {name} ({pid}) is in stock with {stock} units available at ${price:.2f}."
            else:
                return f"Product {name} ({pid}) is carried in our catalog but is currently out of stock."

    return f"Product '{product_id}' is not carried in the catalog."


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
