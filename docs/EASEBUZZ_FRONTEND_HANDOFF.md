# Easebuzz frontend integration handoff

Backend base URL: `https://kioskai.greenfibre.org`

The payment patch has been applied on Ubuntu and 66 mocked payment tests passed.
The last supplied readiness report failed production environment/authentication
and kiosk allowlist checks. The running service has not been confirmed to load
the patch. Greenfibre Easebuzz support and live merchant credentials are not yet
verified. Resolve these dependencies before customer payment testing.

## Authentication and frontend configuration

The frontend team must provide its exact browser origin for the backend's
`CORS_ORIGINS` allowlist. The backend hostname does not identify that origin.
Use HTTPS and the assigned, registered kiosk ID consistently.

The trusted gateway/controller obtains tickets using this server-side request:

```http
POST /auth/kiosk/token
Content-Type: application/json
X-Gateway-Key: <private gateway credential held by trusted server>

{"kiosk_id":"<registered kiosk ID>"}
```

It receives `ticket`, `kiosk_id`, `expires_in` and `ticket_type`. The expiry is
configurable (default 300 seconds). The gateway must authenticate/authorize its
own kiosk clients before distributing tickets; do not build an unrestricted
ticket proxy. Only the short-lived ticket goes to the browser. Renew it through
the trusted gateway when it expires, preserving session and checkout identity.
The ticket issuing endpoint is `/auth/kiosk/token`, without a `/v1` prefix.

Send these headers on all frontend payment requests:

```http
Content-Type: application/json
X-Kiosk-ID: <registered kiosk ID>
X-Kiosk-Token: <short-lived signed ticket>
```

`Authorization: Bearer <ticket>` is also accepted instead of `X-Kiosk-Token`.
Keep tickets in memory and out of URLs/logs. Do not embed `KIOSK_GATEWAY_KEY`,
`KIOSK_AUTH_SECRET`, `KIOSK_ORDERS_API_KEY`, or Easebuzz key/salt in browser code,
public build variables or frontend requests. The browser does not call Greenfibre's
private kiosk order API or Easebuzz initiation API directly.

## Endpoint contract

All paths below are relative to the backend base URL. Payment routes also have
equivalent `/v1/payments/...` aliases. Use one prefix consistently.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | API health; does not prove payment readiness |
| POST | `/payments/orders` | Create/retrieve saved checkout; HTTP 201 |
| GET | `/payments/orders/{database_order_id}?session_id={session_id}` | Read saved status; HTTP 200 |
| POST | `/payments/orders/{database_order_id}/reconcile` | Request backend payment reconciliation; HTTP 200 |
| POST | `/payments/verify` | Alternative entry point for the same reconciliation; HTTP 200 |

### 1. Start checkout

Generate a single idempotency key per checkout attempt (for example,
`crypto.randomUUID()`) and send it as `Idempotency-Key`. Keep it and the exact
payload for transport retries. Use the current conversation `session_id` and
actual selected variant SKUs, stock quantities and validated customer details.

```json
{
  "session_id": "<current session ID>",
  "payment_provider": "easebuzz",
  "fulfillment_method": "delivery",
  "customer": {
    "name": "<customer name>",
    "email": "<customer email>",
    "phone": "<customer phone>"
  },
  "shipping_address": {
    "streetAddress": "<street address>",
    "city": "<city>",
    "state": "<state>",
    "pincode": "<six digit pincode>",
    "landmark": ""
  },
  "items": [
    {"sku": "<selected variant SKU>", "quantity": 1}
  ]
}
```

Do not send prices, totals, currency overrides or a `paid` field. The backend
calculates prices/discounts. Optional `is_pair_up` may be sent on an item only
when that feature is supported by Greenfibre; the backend validates eligibility.
The cart accepts 1–20 items with integer quantities 1–100 each.

Illustrative response (values are examples, not live order data):

```json
{
  "saved_to_database": true,
  "database_order_id": "aaaaaaaaaaaaaaaaaaaaaaaa",
  "payment_provider": "easebuzz",
  "merchant_txnid": "K123abc",
  "payment_url": "https://pay.easebuzz.in/pay/exampleAccessKey",
  "amount": 24690,
  "currency": "INR",
  "paid": false,
  "session_id": "<current session ID>"
}
```

`amount` is integer paise: `24690` means INR 246.90. Save the database order ID,
session ID and checkout idempotency key before navigating away. Isolate this
state to the current kiosk/customer and clear it when that customer session ends;
avoid persisting contact information or tickets in shared browser storage.

### 2. Open hosted checkout

Navigate to the returned `payment_url` or open it in the approved kiosk payment
window. Do not launch Razorpay JavaScript or generate an unrelated UPI QR.
The success/failure return route and redirect page are configured by Greenfibre;
the frontend result-page URL must be agreed with that backend team. This kiosk
controller does not accept arbitrary return URLs in checkout requests.

### 3. Recover saved status after return/reload

Use authenticated GET status with the saved order/session IDs. After checkout
return, trigger reconciliation with:

```http
POST /payments/orders/aaaaaaaaaaaaaaaaaaaaaaaa/reconcile
Content-Type: application/json
X-Kiosk-ID: <registered kiosk ID>
X-Kiosk-Token: <ticket>

{"session_id":"<same session ID>"}
```

Alternatively, POST `/payments/verify` with:

```json
{"database_order_id":"aaaaaaaaaaaaaaaaaaaaaaaa","session_id":"<same session ID>"}
```

A 200 response can still contain `paid: false`. Poll the GET status endpoint
using bounded backoff (for example 2, 3, 5 then 10 seconds, stopping after 90
seconds). Stop polling on completion or session reset. After a poll timeout,
show a pending/recheck state, retain the order reference, and avoid asking the
customer to pay again automatically. Greenfibre's worker must recover lost
callbacks independently of the browser.

Show payment success only when saved status returns `paid === true`.
If `stock_conflict` is true, show payment received but order needs assistance;
do not promise fulfillment. Email status is separate from payment status.
Never infer success from a URL parameter, a closed window or an HTTP 200 alone.

## Error handling and acceptance tests

| HTTP status | Frontend behavior |
| --- | --- |
| 401 | Renew/check kiosk ticket through the trusted gateway; an upstream integration credential failure may also be relayed as 401 |
| 403 | Check kiosk authorization/session ownership; escalate if a valid ticket is denied |
| 404 | Check saved order/session/kiosk identity; do not create another payment automatically |
| 409 | Order/idempotency/stock conflict; show actionable error and review cart/order |
| 422 | Fix request fields, contact/address details or missing idempotency key |
| 429 | Back off; reuse checkout key and payload if retrying creation |
| 500/502/503/504 or network timeout | Show temporary failure/pending; preserve existing order and exact checkout retry identity |

Before real customer use, test success, cancellation, failed payment, duplicate
clicks, transport timeout, expired ticket, browser return/reload, wrong kiosk/session
access and pending payment recovery. Validate actual orders/inventory and callback
recovery with the Greenfibre team. Production `EASEBUZZ_ENV=prod` payments move
real money; use a coordinated low-value live transaction only after sandbox and
production readiness are confirmed.
