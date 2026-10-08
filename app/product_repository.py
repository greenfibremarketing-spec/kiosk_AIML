"""Read-only catalogue boundary. JSON is a development snapshot, not live inventory."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Protocol


class ProductRepository(Protocol):
    def catalog(self) -> dict: ...
    def products(self) -> list[dict]: ...
    def bundles(self) -> list[dict]: ...
    def get(self, identifier: str) -> dict | None: ...


class JsonProductRepository:
    def __init__(self, path: str):
        self.path = Path(path)

    def catalog(self) -> dict:
        # Do not retain snapshot prices indefinitely in a singleton cache.
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or any(
            not isinstance(data.get(key, []), list)
            or any(not isinstance(row, dict) for row in data.get(key, []))
            for key in ('products', 'gift_bundles')
        ):
            raise ValueError('Invalid product catalogue structure')
        return data

    def products(self) -> list[dict]:
        return self.catalog().get('products', [])

    def bundles(self) -> list[dict]:
        return self.catalog().get('gift_bundles', [])

    def get(self, identifier: str) -> dict | None:
        identifier = identifier.strip().casefold()
        if not identifier:
            return None
        matches = [p for p in self.products() if identifier in {
            str(p.get(key, '')).casefold() for key in ('id', 'sku', 'name')
        }]
        return matches[0] if len(matches) == 1 else None


def valid_price(value) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        number = Decimal(str(value))
        return number.is_finite() and number >= 0
    except (InvalidOperation, ValueError):
        return False


def is_active(product: dict) -> bool:
    # Missing activity is supported only for the legacy development JSON schema.
    return product.get('isActive', True) is True and product.get('active', True) is True


def stock_state(product: dict, max_age_seconds: int = 300) -> tuple[str, int | None]:
    if not is_active(product):
        return 'inactive', None
    stock = product.get('stock')
    if type(stock) is not int or stock < 0:
        return 'unknown', None
    try:
        observed = datetime.fromisoformat(product['stock_checked_at'].replace('Z', '+00:00'))
        if observed.tzinfo is None:
            return 'unknown', None
        age = (datetime.now(timezone.utc) - observed).total_seconds()
        if not 0 <= age <= max_age_seconds:
            return 'unknown', None
    except (KeyError, TypeError, ValueError, AttributeError):
        return 'unknown', None
    if 'in_stock' in product and (
        type(product['in_stock']) is not bool or product['in_stock'] != (stock > 0)
    ):
        return 'unknown', None
    return ('in_stock' if stock > 0 else 'out_of_stock'), stock


def product_view(product: dict) -> dict:
    status, stock = stock_state(product)
    return {
        'id': product.get('id'), 'sku': product.get('sku'),
        'name': product.get('name', 'Product'), 'category': product.get('category'),
        'price': product.get('price') if valid_price(product.get('price')) else None,
        'mrp': product.get('mrp') if valid_price(product.get('mrp')) else None,
        'stock': stock, 'stock_status': status,
        'in_stock': status == 'in_stock' if status in ('in_stock', 'out_of_stock') else None,
        'description': product.get('description', ''),
        'source': 'development_json', 'price_status': 'catalogue_snapshot',
        'certifications': [],  # No SKU approval registry exists in the JSON adapter.
    }
