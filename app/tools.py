"""Read-only product tools; keep legacy tool names and argument contracts."""

import json
import re
from decimal import Decimal
from typing import Optional
from langchain_core.tools import tool
from app.config import settings
from app.product_repository import (
    JsonProductRepository, ProductRepository, is_active, product_view, valid_price,
)


def get_product_repository() -> ProductRepository:
    """Dependency boundary for a later verified business-service adapter."""
    return JsonProductRepository(settings.products_file)


def _load_catalog() -> dict:
    """Retain the legacy /products catalogue shape."""
    return get_product_repository().catalog()


def clear_catalog_cache() -> None:
    """Compatibility hook; the JSON adapter now reads fresh snapshots on each call."""


@tool
def search_products(query: str = '', category: str = '', max_price: float = 0.0) -> str:
    """Search active catalogue products. Null price/stock means unknown.

    max_price is in INR; zero means no budget filter. Snapshot prices require
    confirmation before checkout. This tool does not verify certifications.
    """
    if not valid_price(max_price):
        return 'Please provide a finite, non-negative budget.'
    stopwords = {'a', 'an', 'the', 'do', 'you', 'have', 'sell', 'show', 'me',
                 'what', 'is', 'of', 'for', 'please', 'price', 'cost', 'much', 'how'}
    words = [w for w in re.findall(r'\w+', query.lower()) if len(w) > 1 and w not in stopwords]
    matches = []
    for product in get_product_repository().products():
        if not is_active(product):
            continue
        text = ' '.join(str(product.get(k, '')) for k in ('name', 'category', 'description')).lower()
        if words and not any(word in text for word in words):
            continue
        if category and category.lower().strip() not in str(product.get('category', '')).lower():
            continue
        price = product.get('price')
        if max_price > 0 and (not valid_price(price) or Decimal(str(price)) > Decimal(str(max_price))):
            continue
        matches.append(product_view(product))
    return json.dumps(matches[:5]) if matches else 'No matching active products were found in the catalogue.'


@tool
def get_product_details(product_id: str) -> str:
    """Get product facts by exact ID, SKU or name. Unknown data is never invented."""
    product = get_product_repository().get(product_id)
    if product is None:
        return 'Product is not available in the catalogue.'
    return json.dumps(product_view(product))


@tool
def check_stock(product_id: str) -> str:
    """Check exact SKU availability; missing, stale or conflicting stock is unknown."""
    product = get_product_repository().get(product_id)
    if product is None:
        return 'Product is not available in the catalogue.'
    view = product_view(product)
    name = view['name']
    if view['stock_status'] == 'inactive':
        return f'{name} is inactive and cannot currently be recommended.'
    if view['stock_status'] == 'unknown':
        return f'Current stock for {name} is unknown. Please confirm availability with a store associate.'
    if view['stock_status'] == 'out_of_stock':
        return f'{name} is currently out of stock.'
    return f'{name} is in stock with {view["stock"]} units available.'


@tool
def get_gift_bundles(budget: Optional[float] = None) -> str:
    """Find active gift bundles with known catalogue prices within an INR budget."""
    if budget is not None and not valid_price(budget):
        return 'Please provide a finite, non-negative budget.'
    bundles = []
    for bundle in get_product_repository().bundles():
        if not is_active(bundle) or not valid_price(bundle.get('price')):
            continue
        if budget is not None and Decimal(str(bundle['price'])) > Decimal(str(budget)):
            continue
        bundles.append({**product_view(bundle), 'items': bundle.get('items', []),
                        'regular_value': bundle.get('regular_value') if valid_price(bundle.get('regular_value')) else None})
    return json.dumps(bundles) if bundles else 'No active gift bundles with known catalogue pricing match this budget.'


ALL_TOOLS = [search_products, get_product_details, check_stock, get_gift_bundles]
