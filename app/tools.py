"""Product catalog tools for Greenie AI Kiosk Agent.

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


def clear_catalog_cache() -> None:
    """Clear memory cache when products.json is reloaded."""
    global _products_data_cache
    _products_data_cache = None


@tool
def search_products(query: str = "", category: str = "", max_price: float = 0.0) -> str:
    """Search for Greenie eco-friendly products by name, keyword, category, or max price in INR.
    
    Args:
        query: Search term (e.g., 'mug', 'bottle', 'tumbler', 'bowl', 'drinkware').
        category: Optional category filter (e.g., 'Drinkware', 'Kitchen & Dining', 'Desk & Office', 'Corporate Gifts').
        max_price: Optional maximum price in INR (e.g., 500, 800, 1000). Set to 0 for no price limit.
        
    Returns:
        JSON string list of matching products, or a neutral notice if not carried.
    """
    catalog = _load_catalog()
    products = catalog.get("products", [])
    q = (query or "").lower().strip()
    cat_filter = (category or "").lower().strip()

    # Neutral filtering for synthetic or plastic items not carried
    synthetic_keywords = ["plastic", "melamine", "nylon", "polyester", "acrylic", "styrofoam", "leather", "disposable"]
    if any(sk in q for sk in synthetic_keywords):
        return f"No products found matching '{query}'. This item is not carried in the Greenie catalog. Greenie exclusively crafts sustainable lifestyle essentials from 100% upcycled rice-husk biocomposite."

    matches = []
    for p in products:
        name_cat = f"{p.get('name', '')} {p.get('category', '')}".lower()
        desc = p.get('description', '').lower()
        material = p.get('sustainability_notes', '').lower()
        full_text = f"{name_cat} {desc} {material}"

        if q and not any(word in full_text for word in q.split() if len(word) > 1):
            continue

        if cat_filter and cat_filter not in p.get("category", "").lower():
            continue

        if max_price > 0 and p.get("price", 0) > max_price:
            continue

        matches.append(p)

    if not matches:
        criteria = f"'{query}'" if query else "the specified criteria"
        return f"No products found matching {criteria}. This item is not carried in the Greenie catalog."

    clean_results = [
        {
            "id": p["id"],
            "name": p["name"],
            "category": p["category"],
            "price": p["price"],
            "mrp": p.get("mrp", p["price"]),
            "stock": p.get("stock", 1 if p.get("in_stock", True) else 0),
            "in_stock": p.get("in_stock", True),
            "description": p["description"],
        }
        for p in matches[:5]
    ]
    return json.dumps(clean_results, indent=2)


@tool
def get_product_details(product_id: str) -> str:
    """Retrieve complete verified details for a specific product by its ID or exact name.
    
    Args:
        product_id: The unique product identifier (e.g., 'viora-bottle', 'statement-mug', 'origin-tumbler', 'flora-bowl').
        
    Returns:
        JSON string with name, price, MRP, stock, description, and sustainability notes.
    """
    catalog = _load_catalog()
    products = catalog.get("products", [])
    pid = product_id.lower().strip()

    for p in products:
        if p["id"].lower() == pid or pid in p["name"].lower():
            return json.dumps(p, indent=2)

    return f"Product '{product_id}' is not carried in the catalog."


@tool
def check_stock(product_id: str) -> str:
    """Check inventory availability and stock level for a product ID or product name.
    
    Args:
        product_id: The unique product identifier (e.g., 'viora-bottle', 'statement-mug', 'origin-tumbler', 'flora-bowl').
        
    Returns:
        Neutral status string distinguishing in-stock, out of stock, or not carried.
    """
    catalog = _load_catalog()
    products = catalog.get("products", [])
    pid = product_id.lower().strip()

    for p in products:
        if p["id"].lower() == pid or pid in p["name"].lower():
            stock = p.get("stock", 0)
            in_stock = p.get("in_stock", stock > 0)
            name = p.get("name", "Product")
            price = p.get("price", 0)
            if in_stock and stock > 0:
                return f"Product {name} ({p['id']}) is in stock with {stock} units available at ₹{price}."
            elif in_stock:
                return f"Product {name} ({p['id']}) is in stock at ₹{price}."
            else:
                return f"Product {name} ({p['id']}) is carried in our catalog but is currently out of stock."

    return f"Product '{product_id}' is not carried in the catalog."


@tool
def get_gift_bundles(budget: Optional[float] = None) -> str:
    """Find curated eco-friendly gift bundles and corporate hampers, optionally filtered by maximum budget in INR.
    
    Args:
        budget: Optional maximum budget amount in INR (e.g., 1000, 1500, 2000).
        
    Returns:
        JSON string list of suitable gift bundles with pricing in INR, included items, and savings.
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
                "mrp": b.get("mrp"),
                "regular_value": b.get("regular_value"),
                "items": b["items"],
                "description": b["description"],
            })

    if not results:
        if budget is not None:
            return f"No gift bundles found within budget of ₹{int(budget)}. The lowest price bundle is ₹949."
        return "No gift bundles currently available."

    return json.dumps(results, indent=2)


# Export tool list
ALL_TOOLS = [search_products, get_product_details, check_stock, get_gift_bundles]
