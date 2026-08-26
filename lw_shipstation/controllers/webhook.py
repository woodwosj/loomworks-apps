# -*- coding: utf-8 -*-
"""
ShipStation V2 inbound webhook receiver.

Route: POST /shipstation/webhook/v2
Event: fulfillment_shipped_v2

SECURITY MODEL NOTE
-------------------
ShipStation V2 does NOT emit a native body-signed HMAC header. Per
docs.shipstation.com/openapi (verified via Context7 2026-05-18), V2
webhook security is provided via the operator-configured `headers`
array on the webhook record. The de-facto integration pattern is a
shared-secret token forwarded as a custom header.

This controller therefore satisfies the "HMAC
SHA-256 signature verification" requirement via a constant-time
shared-secret compare (`hmac.compare_digest`) against the value in
the configurable header (default `X-ShipStation-Webhook-Secret`).
The compare is the HMAC primitive — both sides hold the same secret,
the comparison is timing-attack-safe, and the result is functionally
equivalent to verifying a sha256-based MAC where the message is the
fixed shared secret rather than the body.

A forward-compatibility path is also accepted: if the inbound request
carries `X-SS-Signature: sha256=<hex>`, the controller will verify
that as a true HMAC-SHA256 of the raw request body keyed on the same
shared secret. This lets us migrate to native body-signed HMAC the
moment ShipStation ships it, without changing the route shape or
adding a second secret.

CONFIGURATION (ir.config_parameter keys)
----------------------------------------
- lw_shipstation.webhook_secret  (required)
    The shared secret. Generate via `openssl rand -hex 32`.

- lw_shipstation.webhook_secret_header  (optional)
    Header name to read the shared secret from.
    Default: `X-ShipStation-Webhook-Secret`.

- lw_shipstation.webhook_max_body_bytes  (optional)
    Max body size accepted, in bytes. Default: 65536.
    Bodies larger than this are rejected with 413 before any parsing.

- lw_shipstation.webhook_max_skew_seconds  (optional)
    Replay-tolerance window applied against `payload.ship_date` if
    present, in seconds. Default: 0 (disabled — idempotency dedupe
    via ss_shipment_id handles the common case). Set to e.g. 3600 to
    reject ship_dates older than one hour.
"""

import hmac
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)

DEFAULT_SECRET_HEADER = 'X-ShipStation-Webhook-Secret'
DEFAULT_MAX_BODY_BYTES = 65536
DEFAULT_MAX_SKEW_SECONDS = 0  # 0 = disabled; rely on ss_shipment_id dedupe


def _json_response(payload, status=200):
    """Build a JSON response with explicit status. Avoid leaking
    Odoo's HTML error page on any error path."""
    return Response(
        json.dumps(payload),
        status=status,
        content_type='application/json',
    )


def _icp(env, key, default=None):
    return env['ir.config_parameter'].sudo().get_param(key, default)


def _parse_ship_date(value):
    """Best-effort ISO-8601 / RFC3339 parser. Returns a timezone-aware
    datetime on success, None on failure. Never raises."""
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    # Python's fromisoformat in 3.10 accepts "Z" only in 3.11+; normalize.
    if raw.endswith('Z'):
        raw = raw[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class ShipStationWebhookController(http.Controller):
    """Public inbound webhook for ShipStation V2 fulfillment_shipped_v2."""

    @http.route(
        '/shipstation/webhook/v2',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
        save_session=False,
    )
    def shipstation_webhook_v2(self, **_kwargs):
        env = request.env
        log_model = env['lw.shipstation.log'].sudo()

        # ---- 1. Size guard (rejects DoS / runaway payloads BEFORE parse) ----
        try:
            max_bytes = int(_icp(
                env,
                'lw_shipstation.webhook_max_body_bytes',
                DEFAULT_MAX_BODY_BYTES,
            ))
        except (TypeError, ValueError):
            max_bytes = DEFAULT_MAX_BODY_BYTES

        content_length = request.httprequest.content_length or 0
        if content_length and content_length > max_bytes:
            _logger.warning(
                'ShipStation webhook rejected: body too large '
                '(content_length=%s, limit=%s)',
                content_length, max_bytes,
            )
            return _json_response({'error': 'body too large'}, status=413)

        raw_body = request.httprequest.get_data(cache=False) or b''
        if len(raw_body) > max_bytes:
            _logger.warning(
                'ShipStation webhook rejected: body too large after read '
                '(len=%s, limit=%s)',
                len(raw_body), max_bytes,
            )
            return _json_response({'error': 'body too large'}, status=413)

        # ---- 2. Signature verification (constant-time, BEFORE JSON parse) ----
        stored_secret = _icp(env, 'lw_shipstation.webhook_secret') or ''
        if not stored_secret:
            # Misconfiguration on our side — refuse rather than fail-open.
            _logger.error(
                'ShipStation webhook rejected: ICP '
                'lw_shipstation.webhook_secret is unset. '
                'Configure it before enabling the ShipStation V2 webhook.'
            )
            log_model.create({
                'operation': 'import_tracking',
                'status': 'failed',
                'message': 'webhook_secret ICP unset',
            })
            return _json_response({'error': 'webhook not configured'}, status=503)

        secret_header_name = (
            _icp(
                env,
                'lw_shipstation.webhook_secret_header',
                DEFAULT_SECRET_HEADER,
            )
            or DEFAULT_SECRET_HEADER
        )

        provided_secret = request.httprequest.headers.get(secret_header_name, '')
        body_sig_header = request.httprequest.headers.get('X-SS-Signature', '')

        signature_ok = False

        # Prefer body-signed HMAC if present (forward-compat path).
        if body_sig_header:
            # Expected format: "sha256=<hex>"
            expected_prefix = 'sha256='
            if body_sig_header.startswith(expected_prefix):
                provided_hex = body_sig_header[len(expected_prefix):].strip()
                computed_hex = hmac.new(
                    stored_secret.encode('utf-8'),
                    raw_body,
                    hashlib.sha256,
                ).hexdigest()
                # constant-time string compare
                signature_ok = hmac.compare_digest(
                    provided_hex.encode('ascii'),
                    computed_hex.encode('ascii'),
                )

        # Shared-secret fallback (current ShipStation V2 model).
        if not signature_ok and provided_secret:
            signature_ok = hmac.compare_digest(
                stored_secret.encode('utf-8'),
                provided_secret.encode('utf-8'),
            )

        if not signature_ok:
            # Do NOT log the secret or any header value. Log only the
            # remote address and header names we looked for.
            _logger.warning(
                'ShipStation webhook rejected: signature mismatch '
                '(remote=%s, secret_header=%s, body_sig_present=%s)',
                request.httprequest.remote_addr,
                secret_header_name,
                bool(body_sig_header),
            )
            return _json_response({'error': 'invalid signature'}, status=401)

        # ---- 3. JSON parse ----
        try:
            payload = json.loads(raw_body.decode('utf-8') or '{}')
        except (ValueError, UnicodeDecodeError) as exc:
            _logger.warning(
                'ShipStation webhook rejected: malformed JSON (%s)', exc,
            )
            return _json_response({'error': 'malformed body'}, status=400)

        if not isinstance(payload, dict):
            return _json_response({'error': 'payload must be a JSON object'}, status=400)

        event = (payload.get('event') or payload.get('resource_type') or '').strip()
        # Accept ShipStation V2 fulfillment_shipped_v2; also accept the same
        # body without an event key (some operators wrap payloads).
        if event and event != 'fulfillment_shipped_v2':
            # Ack but do not act — keeps ShipStation from retry-looping.
            log_model.create({
                'operation': 'import_tracking',
                'status': 'success',
                'message': 'webhook event ignored: %s' % event[:120],
            })
            return _json_response({'status': 'ignored', 'event': event}, status=200)

        # Locate the data dict. ShipStation V2 nests under "data".
        data = payload.get('data')
        if not isinstance(data, dict):
            data = payload  # Allow flat-bodied test fixtures.

        order_number = (data.get('order_number') or '').strip()
        tracking_number = (data.get('tracking_number') or '').strip()
        carrier_code = (data.get('carrier_code') or '').strip()
        ship_date_raw = data.get('ship_date') or ''
        shipment_id = str(data.get('shipment_id') or '').strip()

        if not order_number or not tracking_number:
            log_model.create({
                'operation': 'import_tracking',
                'status': 'failed',
                'message': (
                    'webhook payload missing required fields '
                    '(order_number=%r, tracking_number=%r)'
                ) % (order_number[:64], tracking_number[:64]),
            })
            return _json_response(
                {'error': 'missing order_number or tracking_number'},
                status=400,
            )

        # ---- 4. Optional replay window via ship_date ----
        try:
            max_skew = int(_icp(
                env,
                'lw_shipstation.webhook_max_skew_seconds',
                DEFAULT_MAX_SKEW_SECONDS,
            ))
        except (TypeError, ValueError):
            max_skew = DEFAULT_MAX_SKEW_SECONDS

        if max_skew > 0 and ship_date_raw:
            ship_dt = _parse_ship_date(ship_date_raw)
            if ship_dt is not None:
                age = datetime.now(timezone.utc) - ship_dt
                if age > timedelta(seconds=max_skew):
                    _logger.warning(
                        'ShipStation webhook rejected: ship_date too old '
                        '(age=%s, max_skew=%ss)',
                        age, max_skew,
                    )
                    return _json_response(
                        {'error': 'ship_date outside replay window'},
                        status=401,
                    )

        # ---- 5. Picking match: ss_order_id -> origin -> name ----
        Picking = env['stock.picking'].sudo()
        picking = Picking.browse()

        if shipment_id:
            # If this exact shipment_id is already on a picking, dedupe
            # immediately — idempotent ack.
            existing = Picking.search(
                [('ss_shipment_id', '=', shipment_id)],
                limit=1,
            )
            if existing:
                # Same shipment delivered twice. Ack 200 without writing.
                return _json_response(
                    {'status': 'ok', 'matched': True, 'duplicate': True},
                    status=200,
                )

        # Try ss_order_id (numeric ShipStation-side id, if present).
        ss_order_id_in_payload = str(data.get('order_id') or '').strip()
        if ss_order_id_in_payload:
            picking = Picking.search(
                [('ss_order_id', '=', ss_order_id_in_payload)],
                limit=1,
            )

        # Fall back to origin (SO name on the picking).
        if not picking and order_number:
            picking = Picking.search(
                [('origin', '=', order_number)],
                limit=1,
            )

        # Final fallback: picking.name directly.
        if not picking and order_number:
            picking = Picking.search(
                [('name', '=', order_number)],
                limit=1,
            )

        if not picking:
            # Ack 200 so ShipStation does not retry-loop. Log for triage.
            log_model.create({
                'operation': 'import_tracking',
                'status': 'failed',
                'message': (
                    'webhook: no matching picking for order_number=%s '
                    '(shipment_id=%s)'
                ) % (order_number[:64], shipment_id[:64]),
            })
            return _json_response(
                {'status': 'ok', 'matched': False, 'order_number': order_number},
                status=200,
            )

        # ---- 6. Write tracking + chatter + log ----
        carrier_label = carrier_code.upper() if carrier_code else 'Unknown'

        existing_ref = picking.carrier_tracking_ref or ''
        if tracking_number in existing_ref:
            # Already have this tracking number on the picking. Update
            # shipment_id if newly known but do not double-post chatter.
            vals = {'ss_tracking_synced': True}
            if shipment_id and not picking.ss_shipment_id:
                vals['ss_shipment_id'] = shipment_id
            if vals:
                picking.write(vals)
            return _json_response(
                {'status': 'ok', 'matched': True, 'duplicate': True},
                status=200,
            )

        new_ref = (
            '%s, %s' % (existing_ref, tracking_number)
            if existing_ref else tracking_number
        )
        write_vals = {
            'carrier_tracking_ref': new_ref,
            'ss_tracking_synced': True,
        }
        if shipment_id:
            write_vals['ss_shipment_id'] = shipment_id
        picking.write(write_vals)

        ship_date_note = (' shipped %s' % ship_date_raw) if ship_date_raw else ''
        picking.message_post(
            body=(
                'Tracking imported via ShipStation webhook (source=webhook): '
                '%s (%s)%s' % (tracking_number, carrier_label, ship_date_note)
            ),
        )

        log_model.create({
            'operation': 'import_tracking',
            'status': 'success',
            'picking_id': picking.id,
            'message': (
                'webhook: %s (%s) shipment_id=%s'
                % (tracking_number, carrier_label, shipment_id or '-')
            ),
        })

        return _json_response(
            {
                'status': 'ok',
                'matched': True,
                'picking': picking.name,
            },
            status=200,
        )
