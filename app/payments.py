"""Kiosk checkout: delegate sales orders to the website, or use standalone Razorpay.

The website owns MongoDB orders, stock changes and confirmation email delivery.
"""

from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import re
from typing import Annotated, Callable, Literal
from uuid import uuid4
from urllib.parse import urlsplit, urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.greenfibre_repository import ProductAPIError
from app.product_repository import is_active, stock_state, valid_price
from app.tools import get_product_repository


SessionID = Annotated[str, Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')]
OrderID = Annotated[str, Field(pattern=r'^order_[A-Za-z0-9]{1,64}$')]
SavedOrderID = Annotated[str, Field(pattern=r'^(order_[A-Za-z0-9]{1,64}|[a-fA-F0-9]{24})$')]
PaymentID = Annotated[str, Field(pattern=r'^pay_[A-Za-z0-9]{1,64}$')]


class CheckoutItem(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    sku: str = Field(min_length=1, max_length=128)
    quantity: int = Field(default=1, strict=True, ge=1, le=100)
    is_pair_up: bool | None = None


class Customer(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(max_length=254, pattern=r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
    phone: str = Field(pattern=r'^[+0-9 ()-]{7,30}$')


class ShippingAddress(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    streetAddress: str = Field(min_length=1, max_length=200)
    city: str = Field(min_length=1, max_length=200)
    state: str = Field(min_length=1, max_length=200)
    pincode: str = Field(pattern=r'^\d{6}$')
    landmark: str = Field(default='', max_length=200)


class CreateOrder(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: SessionID
    items: list[CheckoutItem] = Field(min_length=1, max_length=20)
    customer: Customer | None = None
    shipping_address: ShippingAddress | None = None
    fulfillment_method: str = Field(default='delivery', pattern=r'^delivery$')
    payment_provider: Literal['easebuzz', 'razorpay'] | None = None


class VerifyPayment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: SessionID
    razorpay_order_id: OrderID
    razorpay_payment_id: PaymentID
    razorpay_signature: str = Field(pattern=r'^[a-fA-F0-9]{64}$')


class VerifySavedPayment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: SessionID
    database_order_id: Annotated[str, Field(pattern=r'^[a-fA-F0-9]{24}$')]


class ReconcilePayment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: SessionID


def order_api_url() -> str:
    """Preserve the deployed URL setting; fallback uses the catalogue API base."""
    return settings.kiosk_orders_api_url or settings.greenfibre_api_base_url.rstrip('/') + '/kiosk'


def saved_proof(data: dict, session_id: str, kiosk_id: str, *, order_id=None,
                checkout_provider=None) -> dict:
    """Fail closed on malformed proof; never expose secrets or customer payloads."""
    if (data.get('saved_to_database') is not True
            or not isinstance(data.get('database_order_id'), str)
            or not re.fullmatch(r'[a-fA-F0-9]{24}', data['database_order_id'])
            or type(data.get('amount')) is not int or data['amount'] <= 0
            or data.get('currency') != 'INR' or type(data.get('paid')) is not bool
            or data.get('session_id', session_id) != session_id
            or data.get('kiosk_id', kiosk_id) != kiosk_id
            or (order_id and data['database_order_id'] != order_id)):
        raise HTTPException(502, 'Website returned invalid saved order proof.')
    provider = data.get('payment_provider')
    if provider not in ('easebuzz', 'razorpay', None):
        raise HTTPException(502, 'Website returned an unsupported payment provider.')
    if checkout_provider == 'easebuzz':
        url = data.get('payment_url')
        txnid = data.get('merchant_txnid')
        host = 'pay.easebuzz.in' if settings.easebuzz_env == 'prod' else 'testpay.easebuzz.in'
        # Strict canonical URL: no credentials, query, port or alternate host.
        if (provider != 'easebuzz' or not isinstance(txnid, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', txnid)
                or not isinstance(url, str)
                or not re.fullmatch(r'https://' + re.escape(host) + r'/pay/[A-Za-z0-9_-]+/?', url)):
            raise HTTPException(502, 'Website returned invalid Easebuzz checkout details.')
    fields = ('saved_to_database', 'database_order_id', 'order_number', 'payment_provider',
              'merchant_txnid', 'amount', 'currency', 'paid', 'status', 'order_status',
              'confirmed_at', 'stock_conflict', 'email_status')
    result = {k: data[k] for k in fields if k in data}
    result['session_id'] = session_id
    if checkout_provider == 'easebuzz':
        result['payment_url'] = data['payment_url']
    elif checkout_provider == 'razorpay':
        result.update({k: data[k] for k in ('key_id', 'order_id') if k in data})
    return result


def website_request(method: str, path: str, kiosk_id: str, payload=None, request_id=None) -> dict:
    """Server-to-server only: the integration key is never returned to kiosk clients."""
    url = order_api_url()
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or (parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'))):
        raise HTTPException(503, 'Website order API URL must use HTTPS or local HTTP.')
    secret = settings.kiosk_orders_api_key
    if not secret or len(secret.get_secret_value()) < 32:
        raise HTTPException(503, 'Website order API key is not configured.')
    headers = {'X-API-Key': secret.get_secret_value(), 'X-Kiosk-ID': kiosk_id}
    if request_id:
        headers['Idempotency-Key'] = request_id
    try:
        with httpx.Client(base_url=url.rstrip('/') + '/', headers=headers,
                          timeout=settings.kiosk_orders_timeout_seconds, follow_redirects=False) as client:
            response = client.request(method, path, json=payload)
            if response.status_code in (400, 401, 403, 404, 409, 422, 429, 503):
                raise HTTPException(response.status_code, 'Website order service rejected the request. Check customer details, stock, credentials and order status.')
            if response.status_code not in (200, 201):
                raise ValueError('Unexpected order service response')
            data = response.json()
            if not isinstance(data, dict) or data.get('saved_to_database') is not True:
                raise ValueError('Invalid order proof')
            return data
    except httpx.TimeoutException:
        raise HTTPException(504, 'Order service timed out. Retry with the same Idempotency-Key; do not create a duplicate order.') from None
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, 'Website order service is unavailable.') from None


def credentials() -> tuple[str, str]:
    secret = settings.razorpay_key_secret
    if not settings.razorpay_key_id or not secret or not secret.get_secret_value():
        raise HTTPException(503, 'Razorpay is not configured.')
    return settings.razorpay_key_id, secret.get_secret_value()


def provider_request(method: str, path: str, payload: dict | None = None) -> dict:
    """Bounded requests with sanitized errors; never retry order creation blindly."""
    auth = credentials()
    try:
        with httpx.Client(base_url='https://api.razorpay.com/v1/', auth=auth,
                          timeout=settings.razorpay_timeout_seconds) as client:
            response = client.request(method, path, json=payload)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError('Invalid provider response')
            return data
    except httpx.TimeoutException:
        raise HTTPException(504, 'Payment provider timed out. Check order status before retrying.') from None
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, 'Payment provider is unavailable or rejected the request.') from None


def checkout_total(items: list[CheckoutItem]) -> tuple[int, list[dict]]:
    repo = get_product_repository()
    lines = []
    seen = set()
    total = 0
    for item in items:
        try:
            product = repo.get(item.sku)
        except (ProductAPIError, ValueError, OSError):
            raise HTTPException(503, 'Fresh product information is unavailable.') from None
        if not product or not is_active(product):
            raise HTTPException(409, 'A selected product is unavailable.')
        sku = product.get('sku')
        if not sku or sku in seen:
            raise HTTPException(422, 'Use a single cart entry per product SKU.')
        seen.add(sku)
        # Development JSON prices must never be used to collect live money.
        if product.get('source') != 'greenfibre_api' or product.get('price_status') != 'fresh_api':
            raise HTTPException(409, 'Checkout requires fresh catalog prices and inventory.')
        if product.get('variants') and not product.get('variant_id'):
            raise HTTPException(409, 'Select a product variant before checkout.')
        status, stock = stock_state(product)
        if status != 'in_stock' or stock < item.quantity:
            raise HTTPException(409, 'Insufficient verified stock for a selected product.')
        if product.get('currency') != 'INR' or not valid_price(product.get('price')):
            raise HTTPException(409, 'A selected product has no valid INR price.')
        try:
            paise = Decimal(str(product['price'])) * 100
            if paise <= 0 or paise != paise.to_integral_value():
                raise ValueError('Invalid price precision')
            unit_amount = int(paise)
        except (InvalidOperation, ValueError, OverflowError):
            raise HTTPException(409, 'A selected product has an invalid price.') from None
        total += unit_amount * item.quantity
        lines.append({'sku': sku, 'quantity': item.quantity, 'unit_amount': unit_amount})
    if not 100 <= total <= 100000000:
        raise HTTPException(422, 'Checkout total must be between INR 1 and INR 1,000,000.')
    return total, lines


def owned_order(order_id: str, session_id: str, kiosk_id: str) -> dict:
    order = provider_request('GET', f'orders/{order_id}')
    notes = order.get('notes')
    if (order.get('id') != order_id or not isinstance(notes, dict)
            or notes.get('integration') != 'greeny_kiosk'
            or notes.get('session_id') != session_id or notes.get('kiosk_id') != kiosk_id):
        raise HTTPException(404, 'Payment order not found for this kiosk and session.')
    if (type(order.get('amount')) is not int or order['amount'] < 100
            or order.get('currency') != 'INR'):
        raise HTTPException(502, 'Invalid payment order returned by provider.')
    return order


def order_status(order: dict) -> dict:
    paid = (order.get('status') == 'paid'
            and order.get('amount_paid') == order['amount']
            and order.get('amount_due') == 0)
    return {'order_id': order['id'], 'amount': order['amount'], 'currency': order['currency'],
            'status': 'paid' if paid else 'pending', 'paid': paid}


def create_payment_router(authenticate: Callable) -> APIRouter:
    router = APIRouter(prefix='/payments', tags=['payments'])

    @router.post('/orders', status_code=201)
    def create_order(body: CreateOrder, request: Request):
        kiosk_id = authenticate(request)
        provider = body.payment_provider or settings.payment_provider
        # Never silently fall back to Razorpay after an Easebuzz failure.
        if provider == 'easebuzz' or settings.kiosk_orders_api_url:
            request_id = request.headers.get('Idempotency-Key', '')
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request_id):
                raise HTTPException(422, 'Idempotency-Key header is required for saved orders.')
            if not body.customer or (body.fulfillment_method == 'delivery' and not body.shipping_address):
                raise HTTPException(422, 'Customer name, email, phone and delivery address are required.')
            payload = body.model_dump(exclude_none=True)
            if provider == 'easebuzz':
                payload['payment_provider'] = 'easebuzz'
            else:
                # Preserve the deployed Razorpay website contract.
                payload.pop('payment_provider', None)
            data = website_request('POST', 'orders', kiosk_id, payload, request_id)
            return saved_proof(data, body.session_id, kiosk_id, checkout_provider=provider)
        key_id, _ = credentials()
        amount, lines = checkout_total(body.items)
        receipt = 'kiosk_' + uuid4().hex
        # Cart data stays in provider notes, so verification survives restarts/workers.
        notes = {'integration': 'greeny_kiosk', 'kiosk_id': kiosk_id,
                 'session_id': body.session_id}
        # Razorpay notes allow 15 keys, 256 characters each. Compact cart groups.
        cart = json.dumps(lines, separators=(',', ':'))
        if len(cart) > 256 * 12:
            raise HTTPException(422, 'Cart is too large for checkout. Split into smaller orders.')
        notes.update({f'cart_{i // 256}': cart[i:i + 256] for i in range(0, len(cart), 256)})
        order = provider_request('POST', 'orders', {
            'amount': amount, 'currency': 'INR', 'receipt': receipt,
            'partial_payment': False, 'notes': notes,
        })
        if (not isinstance(order.get('id'), str) or not order['id'].startswith('order_')
                or order.get('amount') != amount or order.get('currency') != 'INR'):
            raise HTTPException(502, 'Invalid payment order returned by provider.')
        return {'key_id': key_id, 'order_id': order['id'], 'amount': amount,
                'currency': 'INR', 'receipt': receipt, 'items': lines, 'status': 'created'}

    @router.post('/verify')
    def verify_payment(body: VerifySavedPayment | VerifyPayment, request: Request):
        kiosk_id = authenticate(request)
        if isinstance(body, VerifySavedPayment):
            data = website_request('POST', f'orders/{body.database_order_id}/reconcile',
                                   kiosk_id, {'session_id': body.session_id})
            return saved_proof(data, body.session_id, kiosk_id, order_id=body.database_order_id)
        if settings.kiosk_orders_api_url:
            data = website_request('POST', 'payments/verify', kiosk_id, body.model_dump())
            return saved_proof(data, body.session_id, kiosk_id)
        _, secret = credentials()
        order = owned_order(body.razorpay_order_id, body.session_id, kiosk_id)
        expected = hmac.new(secret.encode(),
                            f"{order['id']}|{body.razorpay_payment_id}".encode(),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, body.razorpay_signature.lower()):
            raise HTTPException(400, 'Invalid payment signature.')
        payment = provider_request('GET', f'payments/{body.razorpay_payment_id}')
        if (payment.get('id') != body.razorpay_payment_id
                or payment.get('order_id') != order['id']
                or payment.get('amount') != order['amount']
                or payment.get('currency') != order['currency']):
            raise HTTPException(409, 'Payment does not match the checkout order.')
        # Authorized payments are pending until captured; dashboard auto-capture is required.
        paid = payment.get('status') == 'captured' and payment.get('captured') is True
        return {'order_id': order['id'], 'payment_id': payment['id'],
                'amount': order['amount'], 'currency': order['currency'],
                'status': 'paid' if paid else 'pending', 'paid': paid}

    @router.get('/orders/{order_id}')
    def get_order(order_id: SavedOrderID, session_id: SessionID, request: Request):
        kiosk_id = authenticate(request)
        if settings.kiosk_orders_api_url or not order_id.startswith('order_'):
            data = website_request('GET', f'orders/{order_id}?' + urlencode({'session_id': session_id}), kiosk_id)
            return saved_proof(data, session_id, kiosk_id, order_id=order_id)
        if not order_id.startswith('order_'):
            raise HTTPException(422, 'Database order lookup requires the website order service.')
        return order_status(owned_order(order_id, session_id, kiosk_id))

    @router.post('/orders/{database_order_id}/reconcile')
    def reconcile_order(database_order_id: Annotated[str, Field(pattern=r'^[a-fA-F0-9]{24}$')],
                        request: Request, session_id: SessionID | None = None,
                        body: ReconcilePayment | None = None):
        kiosk_id = authenticate(request)
        if body and session_id and body.session_id != session_id:
            raise HTTPException(422, 'Conflicting session IDs.')
        sid = body.session_id if body else session_id
        if not sid:
            raise HTTPException(422, 'session_id is required.')
        data = website_request('POST', f'orders/{database_order_id}/reconcile', kiosk_id, {'session_id': sid})
        return saved_proof(data, sid, kiosk_id, order_id=database_order_id)

    return router


def authenticate_payment(request: Request) -> str:
    from app.server import verify_kiosk_auth
    if not request.headers.get('X-Kiosk-ID'):
        raise HTTPException(401, 'X-Kiosk-ID header is required.')
    # Payment endpoints require actual authentication even during local testing.
    if not settings.kiosk_auth_secret or not settings.kiosk_auth_secret.get_secret_value().strip():
        raise HTTPException(503, 'Kiosk payment authentication is not configured.')
    if not request.headers.get('X-Kiosk-Token') and not request.headers.get('Authorization'):
        raise HTTPException(401, 'Kiosk authentication token is required.')
    kiosk_id = verify_kiosk_auth(request, claimed_kiosk_id=request.headers['X-Kiosk-ID'])
    from app.sessions import session_manager
    sid = request.query_params.get('session_id')
    if sid and (owner := session_manager.get_session_kiosk(sid)) and owner != kiosk_id:
        raise HTTPException(403, 'Cross-kiosk session access denied.')
    return kiosk_id


# Support both this checkout's router import and Ubuntu's router factory import.
router = APIRouter()
router.include_router(create_payment_router(authenticate_payment))
router.include_router(create_payment_router(authenticate_payment), prefix='/v1')
