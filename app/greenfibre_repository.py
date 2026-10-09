"""Adapter for observed GET /api/product and /api/product/{id} contracts.

No writes, cookie login, undocumented endpoints or stale-on-error fallback.
Catalogue cache is descriptive; every get() verifies a fresh detail response.
"""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import re
from threading import RLock
import time
from typing import Annotated
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator


class ProductAPIError(RuntimeError):
    """Sanitized domain error; never include response bodies or request headers."""


Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')]


class Category(BaseModel):
    model_config = ConfigDict(extra='ignore')
    name: str = ''
    slug: str = ''


class ColorVariant(BaseModel):
    model_config = ConfigDict(extra='ignore')
    id: Identifier | None = Field(default=None, alias='_id')
    name: str = ''
    stock: int | None = Field(default=None, strict=True, ge=0)
    images: list[str | dict] = Field(default_factory=list)


class WebsiteProduct(BaseModel):
    model_config = ConfigDict(extra='ignore')
    id: Identifier = Field(alias='_id')
    name: str = Field(min_length=1, max_length=500)
    sku: str | None = None
    slug: str = ''
    isActive: StrictBool = False
    discountedPrice: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    originalPrice: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    stockQuantity: int | None = Field(default=None, strict=True, ge=0)
    totalStock: int | None = Field(default=None, strict=True, ge=0)
    colors: list[ColorVariant] = Field(default_factory=list)
    images: list[str | dict] = Field(default_factory=list)
    category: Category | str | None = None
    subCategory: Category | str | None = None
    shortDescription: str = ''
    description: str = ''
    tags: list[str] = Field(default_factory=list)
    giftSetContents: dict = Field(default_factory=dict)
    branding: StrictBool | None = None
    brandingTypes: list[str] = Field(default_factory=list)
    giftPackaging: dict = Field(default_factory=dict)

    @field_validator('discountedPrice', 'originalPrice', mode='before')
    @classmethod
    def no_boolean_price(cls, value):
        if isinstance(value, bool):
            raise ValueError('Boolean price is invalid')
        return value


class Pagination(BaseModel):
    page: int = Field(ge=1, strict=True)
    pages: int = Field(ge=0, strict=True)
    total: int = Field(ge=0, strict=True)
    limit: int = Field(ge=1, strict=True)


class ProductPage(BaseModel):
    success: StrictBool
    products: list[WebsiteProduct]
    pagination: Pagination


class ProductDetail(BaseModel):
    success: StrictBool
    product: WebsiteProduct


def image_urls(images: list) -> list[str]:
    result = []
    for image in images:
        url = image if isinstance(image, str) else next(
            (image[k] for k in ('card', 'original', 'url', 'thumbnail') if image.get(k)), None)
        if isinstance(url, str):
            parsed = urlsplit(url)
            if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password:
                result.append(url)
    return list(dict.fromkeys(result))


class GreenFibreProductRepository:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 8,
                 cache_seconds: float = 30, max_pages: int = 20,
                 transport: httpx.BaseTransport | None = None, clock=time.monotonic):
        url = urlsplit(base_url)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('Product API base must be an HTTPS URL without credentials or query')
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self.max_pages = max_pages
        self.transport = transport
        self.clock = clock
        self._cache: list[WebsiteProduct] | None = None
        self._expires = 0.0
        self._retry_at = 0.0
        self._lock = RLock()

    def clear_cache(self):
        with self._lock:
            self._cache = None
            self._expires = 0.0

    def _request(self, path, params=None, missing_ok=False):
        if self.clock() < self._retry_at:
            raise ProductAPIError('Product service is temporarily rate limited.')
        headers = {'Accept': 'application/json', 'Cache-Control': 'no-cache, no-store'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        try:
            # Own short-lived clients: no leaked sockets across worker/test lifecycles.
            with httpx.Client(timeout=self.timeout, transport=self.transport, follow_redirects=False) as client:
                response = client.get(self.base_url + path, params=params, headers=headers)
        except httpx.HTTPError:
            raise ProductAPIError('Product service connection failed.') from None
        if response.status_code == 429:
            try:
                wait = max(1, min(300, int(response.headers.get('Retry-After', '30'))))
            except ValueError:
                wait = 30
            self._retry_at = self.clock() + wait
            raise ProductAPIError('Product service is temporarily rate limited.')
        if response.status_code in (401, 403):
            raise ProductAPIError('Product service authentication failed.')
        if missing_ok and response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ProductAPIError('Product service is unavailable.')
        # Upstream/proxy-cached responses cannot establish fresh stock.
        if response.headers.get('Age', '0') != '0':
            raise ProductAPIError('Product service returned a stale response.')
        try:
            return response.json()
        except ValueError:
            raise ProductAPIError('Product service returned invalid data.') from None

    def _catalogue(self):
        with self._lock:
            if self._cache is not None and self.clock() < self._expires:
                return deepcopy(self._cache)
            rows = []
            ids = set()
            total = pages = None
            for page_number in range(1, self.max_pages + 1):
                try:
                    page = ProductPage.model_validate(self._request('/product', {'page': page_number, 'limit': 50}))
                except ValidationError:
                    raise ProductAPIError('Product catalogue schema is invalid.') from None
                meta = page.pagination
                if not page.success or meta.page != page_number or meta.pages > self.max_pages:
                    raise ProductAPIError('Product pagination is invalid or exceeds the configured limit.')
                if total is not None and (total != meta.total or pages != meta.pages):
                    raise ProductAPIError('Product catalogue changed during pagination; retry.')
                total, pages = meta.total, meta.pages
                for product in page.products:
                    if product.id in ids:
                        raise ProductAPIError('Product catalogue contains duplicate IDs.')
                    ids.add(product.id)
                    rows.append(product)
                if page_number >= meta.pages:
                    if len(rows) != meta.total:
                        raise ProductAPIError('Product catalogue is incomplete.')
                    self._cache, self._expires = deepcopy(rows), self.clock() + self.cache_seconds
                    return rows
            raise ProductAPIError('Product pagination limit exceeded.')

    def _map(self, product: WebsiteProduct, fresh=False, variant_id=None):
        selected = None
        if variant_id:
            matches = [v for v in product.colors if v.id == variant_id]
            if len(matches) != 1:
                return None
            selected = matches[0]
        stock = None
        if fresh:
            if selected:
                stock = selected.stock
            elif product.colors:
                if all(v.stock is not None for v in product.colors):
                    stock = sum(v.stock for v in product.colors)
            elif product.totalStock is not None and product.stockQuantity is not None:
                stock = product.totalStock if product.totalStock == product.stockQuantity else None
            else:
                stock = product.totalStock if product.totalStock is not None else product.stockQuantity
        category = product.category if isinstance(product.category, Category) else Category()
        subcategory = product.subCategory if isinstance(product.subCategory, Category) else Category()
        variants = [{
            'id': v.id, 'sku': f'GF:{product.id}:{v.id}' if v.id else None,
            'name': v.name, 'images': image_urls(v.images),
            'stock': v.stock if fresh else None,
        } for v in product.colors]
        gift_items = product.giftSetContents.get('products', [])
        is_gift = bool(gift_items) or category.slug == 'gift-boxes' or subcategory.slug == 'mini-gift-sets'
        return {
            'id': product.id, 'sku': f'GF:{product.id}' + (f':{variant_id}' if variant_id else ''),
            'website_sku': product.sku, 'variant_id': variant_id,
            'variant_name': selected.name if selected else None,
            'name': product.name, 'isActive': product.isActive,
            'category': category.name, 'category_slug': category.slug,
            'description': re.sub(r'<[^>]*>', ' ', product.shortDescription or product.description),
            'price': str(product.discountedPrice) if product.discountedPrice is not None else None,
            'mrp': str(product.originalPrice) if product.originalPrice is not None else None,
            'currency': 'INR', 'stock': stock,
            'stock_checked_at': datetime.now(timezone.utc).isoformat() if fresh else None,
            'source': 'greenfibre_api', 'price_status': 'fresh_api' if fresh else 'catalogue_snapshot',
            # Never silently show another color when selected variant has no images.
            'images': image_urls(selected.images) if selected else (
                image_urls(product.images) or (variants[0]['images'] if variants else [])),
            'variants': variants, 'certifications': [], 'tags': product.tags,
            'product_url': 'https://greenfibre.org/shop/' + quote(product.slug, safe='') if product.slug else None,
            'is_gift_set': is_gift, 'items': gift_items,
            'customization_available': product.branding,
            'customization_types': product.brandingTypes,
        }

    def products(self):
        return [self._map(p) for p in self._catalogue() if p.isActive]

    def bundles(self):
        return [p for p in self.products() if p['is_gift_set']]

    def catalog(self):
        products = self.products()
        return {'products': products, 'gift_bundles': [p for p in products if p['is_gift_set']]}

    def get(self, identifier: str):
        identifier = identifier.strip()
        variant = None
        if identifier.startswith('GF:'):
            parts = identifier.split(':')
            if len(parts) not in (2, 3):
                return None
            product_id = parts[1]
            variant = parts[2] if len(parts) == 3 else None
        elif re.fullmatch(r'[a-fA-F0-9]{24}', identifier):
            product_id = identifier
        else:
            matches = [p for p in self._catalogue() if identifier and identifier.casefold() in
                       {p.id.casefold(), p.name.casefold(), p.slug.casefold(), (p.sku or '').casefold()}]
            if len(matches) != 1:
                return None
            product_id = matches[0].id
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', product_id) or (variant is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', variant)):
            return None
        payload = self._request('/product/' + quote(product_id, safe=''), missing_ok=True)
        if payload is None:
            return None
        try:
            detail = ProductDetail.model_validate(payload)
        except ValidationError:
            raise ProductAPIError('Product detail schema is invalid.') from None
        if not detail.success or detail.product.id != product_id:
            raise ProductAPIError('Product detail identity is invalid.')
        return self._map(detail.product, fresh=True, variant_id=variant)
