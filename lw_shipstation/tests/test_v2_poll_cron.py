# -*- coding: utf-8 -*-
"""
Cron-level tests for the V2 poll fallback.

Covers the poll-fallback design notes (§ 4.6):
  (a) cron correctly reconciles a picking whose webhook was missed
  (b) cron skips pickings that already have a tracking number
"""

from unittest import mock

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b'1'  # non-empty so .json() is attempted
        self.text = 'fake'

    def json(self):
        return self._payload


@tagged('-at_install', 'post_install', 'lw_shipstation')
class TestShipStationV2PollCron(TransactionCase):

    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('lw_shipstation.api_key', 'fake-v2-key')
        ICP.set_param('lw_shipstation.last_v2_poll', '')

        partner = self.env['res.partner'].create({
            'name': 'V2 Poll Customer',
            'street': '2 Cron St',
            'city': 'Cronville',
        })
        picking_type = self.env['stock.picking.type'].search(
            [('code', '=', 'outgoing')], limit=1,
        )
        self.picking = self.env['stock.picking'].create({
            'partner_id': partner.id,
            'picking_type_id': picking_type.id,
            'location_id': picking_type.default_location_src_id.id
                or self.env.ref('stock.stock_location_stock').id,
            'location_dest_id': picking_type.default_location_dest_id.id
                or self.env.ref('stock.stock_location_customers').id,
            'origin': 'V2POLL-SO-1',
        })

    def _run_cron_with(self, shipments_payload):
        fake = FakeResponse(200, {'shipments': shipments_payload})
        with mock.patch(
            'odoo.addons.lw_shipstation.models.stock_picking_tracking.requests.get',
            return_value=fake,
        ) as mocked:
            self.env['stock.picking']._cron_shipstation_v2_poll()
        return mocked

    # --- 4.6 (a) ---
    def test_a_cron_reconciles_missed_webhook(self):
        self.assertFalse(self.picking.carrier_tracking_ref)

        mocked = self._run_cron_with([{
            'order_number': 'V2POLL-SO-1',
            'tracking_number': '9405511899223189314121',
            'carrier_code': 'usps',
            'shipment_id': 'se-poll-1',
            'ship_date': '2026-05-18T20:00:00Z',
        }])
        # Confirm the cron actually called the V2 endpoint.
        self.assertTrue(mocked.called)
        call_args = mocked.call_args
        self.assertIn('/v2/shipments', call_args[0][0])
        self.assertEqual(
            call_args[1]['headers'].get('API-Key'), 'fake-v2-key',
        )

        self.picking.invalidate_recordset()
        self.assertEqual(
            self.picking.carrier_tracking_ref, '9405511899223189314121',
        )
        self.assertTrue(self.picking.ss_tracking_synced)
        self.assertEqual(self.picking.ss_shipment_id, 'se-poll-1')

        # Chatter tagged source=poll (not webhook).
        msg = self.env['mail.message'].search([
            ('model', '=', 'stock.picking'),
            ('res_id', '=', self.picking.id),
            ('body', 'ilike', 'source=poll'),
        ], limit=1)
        self.assertTrue(msg, 'cron did not post poll-source chatter')

    # --- 4.6 (b) ---
    def test_b_cron_skips_pickings_already_tracked(self):
        # Pre-populate tracking so the cron should skip the picking.
        self.picking.write({
            'carrier_tracking_ref': '9405511899223189314121',
            'ss_tracking_synced': True,
        })
        msg_count_before = self.env['mail.message'].search_count([
            ('model', '=', 'stock.picking'),
            ('res_id', '=', self.picking.id),
        ])
        log_count_before = self.env['lw.shipstation.log'].search_count([
            ('picking_id', '=', self.picking.id),
        ])

        self._run_cron_with([{
            'order_number': 'V2POLL-SO-1',
            'tracking_number': '9405511899223189314121',  # same number
            'carrier_code': 'usps',
            'shipment_id': 'se-poll-skip',
            'ship_date': '2026-05-18T20:00:00Z',
        }])

        # No new chatter, no new log row.
        msg_count_after = self.env['mail.message'].search_count([
            ('model', '=', 'stock.picking'),
            ('res_id', '=', self.picking.id),
        ])
        log_count_after = self.env['lw.shipstation.log'].search_count([
            ('picking_id', '=', self.picking.id),
        ])
        self.assertEqual(msg_count_before, msg_count_after)
        self.assertEqual(log_count_before, log_count_after)

    # --- Watermark advance ---
    def test_watermark_advances_on_success(self):
        ICP = self.env['ir.config_parameter'].sudo()
        before = ICP.get_param('lw_shipstation.last_v2_poll') or ''
        self._run_cron_with([])  # empty page
        after = ICP.get_param('lw_shipstation.last_v2_poll') or ''
        self.assertNotEqual(before, after, 'watermark was not advanced')
        self.assertTrue(after.endswith('Z'))

    # --- Misconfig: no API key skips ---
    def test_unset_api_key_skips_cron(self):
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('lw_shipstation.api_key', '')
        with mock.patch(
            'odoo.addons.lw_shipstation.models.stock_picking_tracking.requests.get',
        ) as mocked:
            self.env['stock.picking']._cron_shipstation_v2_poll()
            self.assertFalse(
                mocked.called,
                'cron must not call ShipStation when api_key is unset',
            )
