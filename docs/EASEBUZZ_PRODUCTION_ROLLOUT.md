# Easebuzz production rollout

## Production addresses

The public Kiosk_AIML backend is **https://kioskai.greenfibre.org**. Keep the
existing `greenie` systemd service and `/opt/greenie_kiosk` working directory.
No payment-controller hostname change is needed: the reverse proxy forwards
requests from this domain to the existing API on port 5007.

| Purpose | Address |
| --- | --- |
| Kiosk backend health | `https://kioskai.greenfibre.org/health` |
| Create kiosk checkout | `https://kioskai.greenfibre.org/payments/orders` |
| Verify/reconcile after checkout | `https://kioskai.greenfibre.org/payments/verify` |
| Saved order status | `https://kioskai.greenfibre.org/payments/orders/{database_order_id}?session_id=...` |
| Versioned payment endpoints | `https://kioskai.greenfibre.org/v1/payments/...` |
| Greenfibre order service used by this backend | `https://api.greenfibre.org/api/kiosk` |

Configure the frontend's API base URL as `https://kioskai.greenfibre.org`.
The payment create/verify/status requests still require kiosk authentication.
Do not change `KIOSK_ORDERS_API_URL` to the kiosk backend domain: it targets
the separate Greenfibre order service and must not loop back to this API.
Easebuzz callbacks remain on the Greenfibre payment backend, as specified in
the implementation guide; this patch does not add a public gateway callback here.

### HTTPS, reverse proxy and CORS

Confirm DNS for `kioskai.greenfibre.org` points to the Ubuntu server and that the
existing reverse proxy serves a valid TLS certificate for this hostname. Preserve
the existing proxy configuration, including WebSocket upgrade forwarding for
`/ws/...`, and forward API paths unchanged to `http://127.0.0.1:5007` when the proxy
runs on that same host. Use the existing internal upstream if your proxy runs
elsewhere. Do not replace the working service or proxy configuration with a
generic template. `HOST`/`PORT` environment settings do not override an explicit
Gunicorn `--bind` in the systemd `ExecStart` command.

`CORS_ORIGINS` must list the **frontend origins**, not merely this backend's
hostname. The frontend hostname has not been supplied. Obtain its exact origin
(scheme, hostname and optional port; no URL path or trailing slash) from the
frontend team and configure an explicit allowlist on Ubuntu. The current
middleware enables credentials, so do not use `CORS_ORIGINS=*` for this rollout.
If the frontend is actually served from `https://kioskai.greenfibre.org` too,
requests to that same origin do not require cross-origin permission. Setting
the backend hostname alone does not authorize a frontend hosted on another domain.

## What this change does

New checkouts default to `PAYMENT_PROVIDER=easebuzz` and call the Greenfibre
website order service. The website owns prices/discounts, durable payment
attempts, idempotency conflicts, gateway signing/callbacks, reconciliation,
inventory updates and email. The kiosk never substitutes local memory for an
order ledger and never confirms payment from a browser claim.

The controller is based on the deployed `9b1331424b9be5f1f1736efa26e2a666d7260b54`
payment module. Existing Razorpay signature/capture checks remain available for
in-flight standalone payments. Website Razorpay verification still uses the
website service. Keep the old credentials until those payments are resolved.
The original Ubuntu router factory and the local router import are both supported.

## Server environment

Keep existing secrets/settings in `/opt/greenie_kiosk/.env`, which your `greenie`
service loads through `EnvironmentFile`. Add or confirm these on Ubuntu:

```dotenv
PAYMENT_PROVIDER=easebuzz
KIOSK_ORDERS_API_URL=https://api.greenfibre.org/api/kiosk
KIOSK_ORDERS_API_KEY=<same private integration key configured on Greenfibre>
KIOSK_ORDERS_TIMEOUT_SECONDS=20
EASEBUZZ_ENV=prod
# Set CORS_ORIGINS to the confirmed frontend origin(s), not a wildcard.
```

`KIOSK_ORDERS_API_URL` is intentionally supported, matching the deployed controller.
If absent, the URL is derived from `GREENFIBRE_API_BASE_URL` plus `/kiosk`.
Earlier advice that this variable was ignored applied to a different local version.
The shared integration key must contain at least 32 characters. Merchant
`EASEBUZZ_KEY`/`EASEBUZZ_SALT` belong on the Greenfibre backend. Local settings
accept those names but do not use or transmit them; this kiosk delegates signing.
`EASEBUZZ_ENV` selects the allowed checkout URL host; it must match Greenfibre.

Production also requires the existing `ENVIRONMENT=production`, kiosk auth and
gateway secrets, explicit `ALLOWED_KIOSK_IDS`, and shared MongoDB coordination.
The readiness script checks these without displaying secrets. Do not simply
change ENVIRONMENT on a server lacking those existing production dependencies.
Payment configuration errors fail payment requests; they do not enable a fake
fallback or force unrelated chat services to stop.

## Required Greenfibre and frontend behavior

Greenfibre must already implement the supplied Easebuzz implementation guide:

- `POST /api/kiosk/orders` accepts `payment_provider: "easebuzz"`, persists the
  order/attempt before returning, calculates totals, and enforces stable
  `Idempotency-Key` requests including changed-payload conflicts.
- Authenticated GET `/api/kiosk/orders/{database_order_id}?session_id=...` and POST
  `/api/kiosk/orders/{database_order_id}/reconcile` enforce saved kiosk/session
  ownership. Reconcile accepts JSON `{"session_id":"..."}`.
- Payment success is established using verified Easebuzz server-side results,
  with callback authentication, exact amount/identity checks and durable recovery.
- Proof includes `saved_to_database: true`, a 24-hex `database_order_id`, integer
  paise `amount`, `currency: "INR"`, boolean `paid`, and existing order proof fields.
  Checkout includes `payment_provider: "easebuzz"`, `merchant_txnid`, and an
  HTTPS hosted `payment_url` on `pay.easebuzz.in` (or `testpay.easebuzz.in` in test).
- Preserve Razorpay verification/reconciliation for old orders during migration.
- Optional `is_pair_up` cart flags are forwarded when supplied; the website must
  validate discount eligibility and calculate the amount itself.

Kiosk clients retain `/payments/*` and `/v1/payments/*`. Creation requires valid
kiosk auth, customer/address and a stable `Idempotency-Key`; retry identical
requests with the same key after timeouts. The response is HTTP 201. Open
`payment_url` as hosted checkout instead of launching Razorpay JavaScript.
Verify now also accepts `{"database_order_id":"...","session_id":"..."}`;
this triggers backend reconciliation and may still return `paid: false`.
Poll the saved order after return with bounded backoff. Only `paid: true`
permits completion. Browser success fields are rejected.

This repository does not contain the Greenfibre order backend or kiosk frontend.
Their implementation and actual provider responses must be verified before
customer traffic is switched. No mocked test establishes merchant onboarding,
live credential validity, settlement or successful fulfillment.

## Apply narrowly on the supplied Ubuntu release

`deploy/easebuzz-production.patch` is generated against your supplied Ubuntu
commit. It modifies the payment controller/configuration, adjusts the existing
Razorpay tests to explicitly select their provider, and adds Easebuzz tests and
the read-only readiness script. It does not alter `.env`, server.py, RAG, ML data,
service configuration or unrelated modules. It contains no merchant credentials.

Transfer the patch to `/tmp/easebuzz-production.patch`. First save a backup of
the two tracked code files and the server `.env` in an administrator-only location.
Keep the frontend on the existing flow until the dependencies above are verified.
From `/opt/greenie_kiosk`:

```bash
git rev-parse HEAD
git diff -- app/config.py app/payments.py tests/test_payments.py
git apply --check /tmp/easebuzz-production.patch
```

If the check fails, stop and review the actual server changes; do not force the
patch or replace the entire application directory. If it passes:

```bash
git apply /tmp/easebuzz-production.patch
.venv/bin/python -m pytest tests/test_payments.py tests/test_easebuzz_payments.py -q
.venv/bin/python scripts/check_payment_readiness.py
```

The preflight is read-only and makes no network request by default. All checks
passing means configuration is present, not that credentials have been accepted
by either service. For an existing saved order belonging to a known kiosk/session:

```bash
.venv/bin/python scripts/check_payment_readiness.py \
  --order-id EXISTING_24_HEX_ORDER_ID --session-id EXISTING_SESSION --kiosk-id EXISTING_KIOSK
```

This optional mode sends only an authenticated GET and validates saved proof.
It neither creates orders nor reconciles payments. A 401/403 may be integration
authentication/ownership failure; a 404 does not prove the key is valid or invalid.
It cannot verify Easebuzz merchant credentials. Do not invent an order and treat
its 404 as a successful authentication check.

Once backend/frontend staging and production configuration are confirmed, restart
the existing service as part of the approved deployment. Run these commands on
Ubuntu only after the patch tests and required configuration checks pass:

```bash
sudo systemctl restart greenie
sudo systemctl is-active greenie
curl --fail --silent --show-error http://127.0.0.1:5007/health
curl --fail --silent --show-error https://kioskai.greenfibre.org/health
```

The public health check validates the public HTTPS route; it does not validate
Easebuzz credentials or create a payment. If local health succeeds but public
health fails, inspect DNS/TLS/reverse-proxy routing before exposing checkout.
Do not use `curl -k` to bypass certificate errors. If a proxy configuration change
is necessary, validate its configuration before reloading it.

A standalone preflight process reads its own environment and
`.env`; systemd EnvironmentFile/overrides can differ. Service restart is required
to load changed environment settings. Do not paste `systemctl ... Environment`,
raw `/proc/.../environ`, or `.env` contents into chat or logs.

Run a controlled sandbox end-to-end transaction first. A separately authorized
low-value live payment must then confirm order/payment/inventory/email records
and recovery before admitting customers. Disable new checkouts during a partial
rollout. For rollback, switch new checkout selection back to Razorpay where
supported, while keeping callbacks and reconciliation for in-flight Easebuzz
orders active on Greenfibre; do not erase payment records.
