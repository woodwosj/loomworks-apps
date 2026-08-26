# -*- coding: utf-8 -*-
"""
Controller-level tests for the ShipStation V2 webhook receiver.

Covers the webhook design notes (§ 4.5):
  (a) valid HMAC + valid payload succeeds (200, tracking written, chatter, log row)
  (b) invalid HMAC is rejected with 401
  (c) missing order_number is rejected with 400
  (d) unknown order_number is acknowledged with 200 (logged)
Plus idempotency: duplicate shipment_id does not double-post chatter.
"""

import json

from odoo.tests import tagged
from odoo.tests.common import HttpCase


WEBHOOK_PATH = '/shipstation/webhook/v2'
SECRET = 'test-secret-' + 'a' * 48  # arbitrary, matches what we ICP-set below


@tagged('-at_install', 'post_install', 'lw_shipstation')
class TestShipStationWebhook(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ICP = cls.env['ir.config_parameter'].sudo()
        ICP.set_param('lw_shipstation.webhook_secret', SECRET)
        # Force replay window off so ship_date age never causes false rejects.
        ICP.set_param('lw_shipstation.webhook_max_skew_seconds', '0')

        partner = cls.env['res.partner'].create({
            'name': 'WS-C Test Customer',
            'street': '1 Test St',
            'city': 'Testville',
        })
        # Use the demo outgoing picking type if available, else first outgoing.
        picking_type = cls.env['stock.picking.type'].search(
            [('code', '=', 'outgoing')], limit=1,
        )
        cls.assertTrue(picking_type, 'No outgoing picking type available')
        cls.picking = cls.env['stock.picking'].create({
            'partner_id': partner.id,
            'picking_type_id': picking_type.id,
            'location_id': picking_type.default_location_src_id.id
                or cls.env.ref('stock.stock_location_stock').id,
            'location_dest_id': picking_type.default_location_dest_id.id
                or cls.env.ref('stock.stock_location_customers').id,
            'origin': 'WSC-TEST-SO-1',
        })

    def _post(self, body, headers=None):
        """Hit the webhook route on the live HttpCase server."""
        headers = dict(headers or {})
        headers.setdefault('Content-Type', 'application/json')
        return self.url_open(
            WEBHOOK_PATH,
            data=json.dumps(body).encode('utf-8'),
            headers=headers,
            timeout=30,
        )

    def _valid_payload(self, **overrides):
        body = {
            'event': 'fulfillment_shipped_v2',
            'data': {
                'order_number': self.picking.origin,
                'tracking_number': '1Z999AA10123456784',
                'carrier_code': 'ups',
                'ship_date': '2026-05-18T18:00:00Z',
                'shipment_id': 'se-test-123',
            },
        }
        body['data'].update(overrides)
        return body

    # --- 4.5 (a) ---
    def test_a_valid_signature_and_payload_writes_tracking(self):
        resp = self._post(
            self._valid_payload(),
            headers={'X-ShipStation-Webhook-Secret': SECRET},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertEqual(body.get('status'), 'ok')
        self.assertTrue(body.get('matched'))

        self.picking.invalidate_recordset()
        self.assertEqual(
            self.picking.carrier_tracking_ref, '1Z999AA10123456784',
            'tracking number not written to picking',
        )
        self.assertTrue(self.picking.ss_tracking_synced)
        self.assertEqual(self.picking.ss_shipment_id, 'se-test-123')

        # Chatter audit: at least one message_post mentioning 'webhook'.
        messages = self.env['mail.message'].search([
            ('model', '=', 'stock.picking'),
            ('res_id', '=', self.picking.id),
        ])
        bodies = ' '.join(m.body or '' for m in messages)
        self.assertIn('source=webhook', bodies)
        self.assertIn('1Z999AA10123456784', bodies)

        # Log row exists.
        log = self.env['lw.shipstation.log'].search(
            [('picking_id', '=', self.picking.id),
             ('operation', '=', 'import_tracking'),
             ('status', '=', 'success')],
            limit=1,
        )
        self.assertTrue(log, 'success log row missing')

    # --- 4.5 (b) ---
    def test_b_invalid_signature_returns_401(self):
        resp = self._post(
            self._valid_payload(),
            headers={'X-ShipStation-Webhook-Secret': 'WRONG_VALUE'},
        )
        self.assertEqual(resp.status_code, 401)
        body = resp.json()
        self.assertIn('invalid', body.get('error', ''))
        # Tracking must NOT be written.
        self.picking.invalidate_recordset()
        self.assertFalse(self.picking.carrier_tracking_ref)

    def test_b2_missing_signature_returns_401(self):
        resp = self._post(self._valid_payload(), headers={})
        self.assertEqual(resp.status_code, 401)

    # --- 4.5 (c) ---
    def test_c_missing_order_number_returns_400(self):
        body = self._valid_payload(order_number='')
        resp = self._post(
            body,
            headers={'X-ShipStation-Webhook-Secret': SECRET},
        )
        self.assertEqual(resp.status_code, 400)

    def test_c2_missing_tracking_number_returns_400(self):
        body = self._valid_payload(tracking_number='')
        resp = self._post(
            body,
            headers={'X-ShipStation-Webhook-Secret': SECRET},
        )
        self.assertEqual(resp.status_code, 400)

    # --- 4.5 (d) ---
    def test_d_unknown_order_number_returns_200_and_logs(self):
        body = self._valid_payload(order_number='DOES-NOT-EXIST-S99999')
        resp = self._post(
            body,
            headers={'X-ShipStation-Webhook-Secret': SECRET},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json().get('matched'))
        # Log row created.
        log = self.env['lw.shipstation.log'].search(
            [('operation', '=', 'import_tracking'),
             ('status', '=', 'failed'),
             ('message', 'ilike', 'DOES-NOT-EXIST-S99999')],
            limit=1,
        )
        self.assertTrue(log)

    # --- Idempotency: duplicate shipment_id ---
    def test_idempotent_duplicate_shipment_does_not_double_post(self):
        headers = {'X-ShipStation-Webhook-Secret': SECRET}
        # First delivery: writes tracking + chatter + log.
        r1 = self._post(self._valid_payload(), headers=headers)
        self.assertEqual(r1.status_code, 200)
        self.assertFalse(r1.json().get('duplicate'))

        # Count chatter + log baseline.
        msg_count_1 = self.env['mail.message'].search_count([
            ('model', '=', 'stock.picking'),
            ('res_id', '=', self.picking.id),
            ('body', 'ilike', 'source=webhook'),
        ])
        log_count_1 = self.env['lw.shipstation.log'].search_count([
            ('picking_id', '=', self.picking.id),
            ('status', '=', 'success'),
        ])

        # Second delivery — same shipment_id.
        r2 = self._post(self._valid_payload(), headers=headers)
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(
            r2.json().get('duplicate'),
            'second delivery of same shipment_id should report duplicate=True',
        )

        msg_count_2 = self.env['mail.message'].search_count([
            ('model', '=', 'stock.picking'),
            ('res_id', '=', self.picking.id),
            ('body', 'ilike', 'source=webhook'),
        ])
        log_count_2 = self.env['lw.shipstation.log'].search_count([
            ('picking_id', '=', self.picking.id),
            ('status', '=', 'success'),
        ])
        self.assertEqual(msg_count_1, msg_count_2,
                         'chatter posted twice for same shipment_id')
        self.assertEqual(log_count_1, log_count_2,
                         'log row created twice for same shipment_id')

    # --- Misconfiguration: secret unset should refuse (not fail-open) ---
    def test_unconfigured_secret_returns_503(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'lw_shipstation.webhook_secret', '',
        )
        try:
            resp = self._post(
                self._valid_payload(),
                headers={'X-ShipStation-Webhook-Secret': 'whatever'},
            )
            self.assertEqual(resp.status_code, 503)
        finally:
            # Restore for subsequent tests in the same class run.
            self.env['ir.config_parameter'].sudo().set_param(
                'lw_shipstation.webhook_secret', SECRET,
            )
