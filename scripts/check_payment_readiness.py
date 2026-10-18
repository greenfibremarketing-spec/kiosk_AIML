"""Read-only Ubuntu/local payment preflight. Never initiates or reconciles payment."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--order-id', help='Existing saved order ID for a read-only authenticated GET')
    parser.add_argument('--session-id')
    parser.add_argument('--kiosk-id')
    args = parser.parse_args()
    # Do not render validation errors: environment inputs may contain secrets.
    try:
        from app.config import settings
    except Exception:
        print(json.dumps({'configuration_valid': False, 'error': 'Settings failed validation; check environment variable names and types.'}))
        return 1
    url = settings.kiosk_orders_api_url or settings.greenfibre_api_base_url.rstrip('/') + '/kiosk'
    try:
        parsed = urlsplit(url)
        https = (parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment)
    except ValueError:
        https = False
    present = lambda value: bool(value and value.get_secret_value().strip())
    checks = {
        'production_environment': settings.environment == 'production',
        'new_checkout_uses_easebuzz': settings.payment_provider == 'easebuzz',
        'live_easebuzz_environment': settings.easebuzz_env == 'prod',
        'order_api_https': bool(https),
        'integration_key_configured': bool(settings.kiosk_orders_api_key and
            len(settings.kiosk_orders_api_key.get_secret_value()) >= 32),
        'kiosk_auth_secret_configured': present(settings.kiosk_auth_secret),
        'kiosk_gateway_key_configured': present(settings.kiosk_gateway_key),
        'explicit_kiosk_allowlist': bool(settings.allowed_kiosk_ids.strip() and settings.allowed_kiosk_ids.strip() != '*'),
        'shared_mongodb_configured': settings.checkpointer_backend == 'mongodb' and present(settings.greeny_ai_mongo_url),
    }
    report = {
        'checks': checks, 'configuration_passed': all(checks.values()),
        'order_service_authentication': 'not_checked',
        'merchant_credentials_and_live_payment': 'not_verified',
        'source_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in ('app/payments.py', 'app/config.py')},
    }
    if any((args.order_id, args.session_id, args.kiosk_id)):
        if not (args.order_id and args.session_id and args.kiosk_id
                and re.fullmatch(r'[a-fA-F0-9]{24}', args.order_id)):
            parser.error('Provide --order-id (24 hex characters), --session-id and --kiosk-id together.')
        if not all(checks.values()):
            report['order_service_authentication'] = 'skipped_configuration_failed'
        else:
            try:
                import httpx
                with httpx.Client(timeout=settings.kiosk_orders_timeout_seconds, follow_redirects=False) as client:
                    response = client.get(url.rstrip('/') + '/orders/' + args.order_id,
                        params={'session_id': args.session_id}, headers={
                            'X-API-Key': settings.kiosk_orders_api_key.get_secret_value(),
                            'X-Kiosk-ID': args.kiosk_id, 'Cache-Control': 'no-store'})
                report['order_service_http_status'] = response.status_code
                if response.status_code != 200:
                    raise ValueError('Read rejected')
                from app.payments import saved_proof
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError('Invalid proof')
                saved_proof(data, args.session_id, args.kiosk_id, order_id=args.order_id)
                report['order_service_authentication'] = 'accepted_existing_saved_order'
            except Exception:
                report['order_service_authentication'] = 'failed_or_invalid_saved_proof'
    print(json.dumps(report, indent=2))
    return 0 if all(checks.values()) and report['order_service_authentication'] in (
        'not_checked', 'accepted_existing_saved_order') else 1


if __name__ == '__main__':
    raise SystemExit(main())
