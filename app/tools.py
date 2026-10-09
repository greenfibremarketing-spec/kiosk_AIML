"""Read-only product tools; keep legacy tool names and argument contracts."""

import json
import re
from decimal import Decimal
from typing import Optional
from functools import lru_cache, wraps
from langchain_core.tools import tool
from app.config import settings
from app.product_repository import (
    JsonProductRepository, ProductRepository, is_active, product_view, valid_price,
)
from app.greenfibre_repository import GreenFibreProductRepository, ProductAPIError


@lru_cache(maxsize=4)
def _api_repository(base_url, token, timeout, cache_seconds, max_pages):
    return GreenFibreProductRepository(base_url, token, timeout, cache_seconds, max_pages)


def get_product_repository() -> ProductRepository:
    """Dependency boundary for a later verified business-service adapter."""
    if settings.product_source == 'json':
        if settings.llm_provider.strip().lower() != 'mock':
            raise ProductAPIError('The JSON catalogue is available only with the mock model.')
        return JsonProductRepository(settings.products_file)
    return _api_repository(
        settings.greenfibre_api_base_url,
        settings.greenfibre_api_token.get_secret_value() if settings.greenfibre_api_token else None,
        settings.greenfibre_api_timeout_seconds, settings.greenfibre_catalogue_cache_seconds,
        settings.greenfibre_api_max_pages,
    )


def _load_catalog() -> dict:
    """Retain the legacy /products catalogue shape."""
    catalog = get_product_repository().catalog()
    return {**catalog, **{
        key: [{**product_view(p), **{field: p[field] for field in ("items", "is_gift_set", "tags", "regular_value") if field in p}} for p in catalog.get(key, [])]
        for key in ('products', 'gift_bundles')
    }}


def clear_catalog_cache() -> None:
    """Discard cached website repositories; JSON is already uncached."""
    _api_repository.cache_clear()


def safe_product_tool(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ProductAPIError:
            return 'Product information is temporarily unavailable. Please try again or ask a store associate; price and stock cannot be confirmed.'
    return call


@tool
@safe_product_tool
def search_products(query: str = '', category: str = '', max_price: float = 0.0, occasion: str = '') -> str:
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
    repository = get_product_repository()
    for product in repository.products():
        if not is_active(product):
            continue
        text = ' '.join(str(product.get(k, '')) for k in ('name', 'category', 'description')).lower()
        if words and not any(word in text for word in words):
            continue
        if category and category.lower().strip() not in str(product.get('category', '')).lower():
            continue
        if occasion and occasion.casefold() not in ' '.join([
            str(product.get('description', '')), *product.get('tags', [])
        ]).casefold():
            continue
        if product.get('source') == 'greenfibre_api':
            # Cached lists select candidates only. Recheck activity, price and stock.
            product = repository.get(product['sku'])
            if product is None or not is_active(product) or product_view(product)['stock_status'] != 'in_stock':
                continue
        price = product.get('price')
        if max_price > 0 and (not valid_price(price) or Decimal(str(price)) > Decimal(str(max_price))):
            continue
        matches.append(product_view(product))
        if len(matches) == 5:
            break
    return json.dumps(matches[:5]) if matches else 'No matching active products were found in the catalogue.'


@tool
@safe_product_tool
def get_product_details(product_id: str) -> str:
    """Get product facts by exact ID, SKU or name. Unknown data is never invented."""
    product = get_product_repository().get(product_id)
    if product is None:
        return 'Product is not available in the catalogue.'
    return json.dumps(product_view(product))


@tool
@safe_product_tool
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
@safe_product_tool
def get_gift_bundles(budget: Optional[float] = None) -> str:
    """Find active gift bundles with known catalogue prices within an INR budget."""
    if budget is not None and not valid_price(budget):
        return 'Please provide a finite, non-negative budget.'
    bundles = []
    repository = get_product_repository()
    for bundle in repository.bundles():
        if bundle.get('source') == 'greenfibre_api':
            bundle = repository.get(bundle['sku'])
            if bundle is None or not bundle.get('is_gift_set') or product_view(bundle)['stock_status'] != 'in_stock':
                continue
        if not is_active(bundle) or not valid_price(bundle.get('price')):
            continue
        if budget is not None and Decimal(str(bundle['price'])) > Decimal(str(budget)):
            continue
        bundles.append({**product_view(bundle), 'items': bundle.get('items', []),
                        'regular_value': bundle.get('regular_value') if valid_price(bundle.get('regular_value')) else None})
    return json.dumps(bundles) if bundles else 'No active gift bundles with known catalogue pricing match this budget.'


@tool
@safe_product_tool
def prepare_corporate_enquiry(product_id: str, quantity: int, occasion: str = '', customization: str = '') -> str:
    """Prepare an unsubmitted enquiry for an exact product/variant SKU. No lead or order is sent.

    Uses fresh retail facts; does not authorize B2B discounts or promise delivery.
    Do not put customer contact information into these arguments.
    """
    if quantity <= 0 or quantity > 100000:
        return 'Please provide a quantity between 1 and 100000.'
    product = get_product_repository().get(product_id)
    if product is None or not is_active(product):
        return 'The selected product is unavailable.'
    return json.dumps({
        'status': 'draft_not_submitted', 'product': product_view(product),
        'quantity': quantity, 'occasion': occasion[:200], 'customization_request': customization[:500],
        'pricing': 'Corporate pricing, MOQ, customization and delivery require human confirmation.',
    })


@tool
@safe_product_tool
def generate_checkout_handoff(product_id: str) -> str:
    """Return the verified website product-page URL for review/QR rendering, not an order or cart.

    Accept the selected GF product/variant SKU so the exact choice is retained.
    Website variant selection is not encoded in the URL; customer must confirm it there.
    """
    product = get_product_repository().get(product_id)
    if product is None or not is_active(product):
        return 'The selected product is unavailable.'
    view = product_view(product)
    return json.dumps({'action': 'review_product_on_website', 'product': view,
                       'url': product.get('product_url'),
                       'requires_confirmation': True, 'order_created': False})


ALL_TOOLS = [search_products, get_product_details, check_stock, get_gift_bundles,
             prepare_corporate_enquiry, generate_checkout_handoff]
