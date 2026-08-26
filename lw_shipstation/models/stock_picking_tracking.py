import logging
from datetime import datetime, timedelta, timezone

import requests

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

SS_V2_BASE_URL = 'https://api.shipstation.com'


class StockPickingTracking(models.Model):
    _inherit = 'stock.picking'

    @api.model
    def _cron_import_shipstation_tracking(self):
        """Cron job: import tracking numbers from ShipStation shipments."""
        company = self.env.company
        if not company.shipstation_enabled:
            _logger.info('ShipStation tracking cron: integration disabled, skipping.')
            return

        last_sync = company.ss_last_tracking_sync
        if not last_sync:
            last_sync = fields.Datetime.now() - timedelta(days=7)

        sync_from = last_sync.strftime('%Y-%m-%d %H:%M:%S')
        page = 1
        total_updated = 0

        while True:
            params = {
                'createDateStart': sync_from,
                'includeShipmentItems': 'true',
                'page': page,
                'pageSize': 100,
            }
            success, result = company._shipstation_api_call(
                'GET', '/shipments', params=params,
            )

            if not success:
                _logger.error(
                    'ShipStation tracking import failed on page %d: %s',
                    page, result,
                )
                self.env['lw.shipstation.log'].sudo().create({
                    'operation': 'import_tracking',
                    'status': 'failed',
                    'message': f'API call failed on page {page}: {str(result)[:200]}',
                })
                break

            shipments = result.get('shipments', [])
            if not shipments:
                break

            for shipment in shipments:
                order_id = str(shipment.get('orderId', ''))
                order_key = shipment.get('orderKey', '')
                tracking_number = shipment.get('trackingNumber', '')
                carrier_name = shipment.get('carrierCode', '')
                shipment_id = str(shipment.get('shipmentId', ''))

                if not tracking_number:
                    continue

                # Find matching picking: by ss_order_id first, then by ss_order_key
                picking = False
                if order_id:
                    picking = self.search(
                        [('ss_order_id', '=', order_id)], limit=1,
                    )
                if not picking and order_key:
                    picking = self.search(
                        [('ss_order_key', '=', order_key)], limit=1,
                    )

                if not picking:
                    _logger.debug(
                        'ShipStation shipment %s: no matching picking for '
                        'order_id=%s / order_key=%s',
                        shipment_id, order_id, order_key,
                    )
                    continue

                # Append tracking number (comma-separated if multiple shipments)
                existing_ref = picking.carrier_tracking_ref or ''
                if tracking_number not in existing_ref:
                    new_ref = (
                        f'{existing_ref}, {tracking_number}'
                        if existing_ref else tracking_number
                    )
                    picking.write({
                        'carrier_tracking_ref': new_ref,
                        'ss_shipment_id': shipment_id,
                        'ss_tracking_synced': True,
                    })

                    carrier_label = carrier_name.upper() if carrier_name else 'Unknown'
                    picking.message_post(
                        body=f'Tracking number imported: {tracking_number} ({carrier_label})',
                    )

                    self.env['lw.shipstation.log'].sudo().create({
                        'operation': 'import_tracking',
                        'status': 'success',
                        'picking_id': picking.id,
                        'message': f'Tracking {tracking_number} ({carrier_label}) '
                                   f'for shipment {shipment_id}',
                    })
                    total_updated += 1

            # Rate limit safety: if near limit, commit and stop
            # (The API call already logged the warning; we check via the
            #  result headers which are not directly available here, but
            #  the _shipstation_api_call method logs a warning. We rely on
            #  ShipStation returning 429 on next call if truly exhausted.)

            # Check for more pages
            total_pages = result.get('pages', 1)
            if page >= total_pages:
                break
            page += 1

        # Update last sync timestamp
        company.sudo().write({
            'ss_last_tracking_sync': fields.Datetime.now(),
        })

        _logger.info(
            'ShipStation tracking import complete: %d pickings updated.',
            total_updated,
        )

    @api.model
    def _cron_import_shipstation_orders(self):
        """Cron job: import recently-modified ShipStation orders and backfill
        ss_order_id / ss_order_key / ss_exported on matching Odoo pickings.
        Link-only — never creates new SOs or pickings."""
        company = self.env.company
        if not company.shipstation_enabled:
            _logger.info('ShipStation order import cron: integration disabled, skipping.')
            return

        since = company.ss_last_order_import or (fields.Datetime.now() - timedelta(days=7))
        run_at = fields.Datetime.now()
        page = 1
        linked = 0
        skipped = 0

        while True:
            params = {
                'modifyDateStart': since.strftime('%Y-%m-%d %H:%M:%S'),
                'orderStatus': 'awaiting_shipment,shipped,on_hold',
                'pageSize': 500,
                'page': page,
                'sortBy': 'ModifyDate',
                'sortDir': 'ASC',
            }
            success, result = company._shipstation_api_call('GET', '/orders', params=params)
            if not success:
                _logger.error('ShipStation order import failed on page %d: %s', page, result)
                self.env['lw.shipstation.log'].sudo().create({
                    'operation': 'import_orders',
                    'status': 'failed',
                    'message': f'API call failed on page {page}: {str(result)[:200]}',
                })
                return

            orders = result.get('orders', [])
            for order in orders:
                on = (order.get('orderNumber') or '').strip()
                if not on:
                    continue
                picking = self.search([('name', '=', on)], limit=1)
                if not picking:
                    so = self.env['sale.order'].search([('name', '=', on)], limit=1)
                    if so:
                        picking = so.picking_ids.filtered(
                            lambda p: p.picking_type_id.code == 'outgoing'
                        )[:1]
                if not picking:
                    skipped += 1
                    continue

                vals = {}
                if not picking.ss_order_id:
                    vals['ss_order_id'] = str(order.get('orderId', ''))
                if not picking.ss_order_key:
                    vals['ss_order_key'] = order.get('orderKey', '')
                if not picking.ss_exported:
                    vals['ss_exported'] = True
                    vals['ss_export_date'] = run_at
                if vals:
                    picking.write(vals)
                    linked += 1

            total_pages = result.get('totalPages', 1) or result.get('pages', 1)
            if page >= total_pages:
                break
            page += 1

        company.sudo().write({'ss_last_order_import': run_at})
        self.env['lw.shipstation.log'].sudo().create({
            'operation': 'import_orders',
            'status': 'success',
            'message': f'Linked {linked} pickings, skipped {skipped} unmatched orders since {since.strftime("%Y-%m-%d %H:%M")}',
        })
        _logger.info(
            'ShipStation order import complete: linked=%d skipped=%d', linked, skipped
        )

    @api.model
    def _cron_shipstation_v2_poll(self):
        """ShipStation V2 poll fallback. Runs every 30 minutes alongside
        the legacy V1 crons. Hits GET /v2/shipments?shipped_after=<watermark>
        and reconciles any matching picking whose tracking is still null.

        Uses the V2 API-Key header authentication (separate from the V1
        Basic-auth credentials on res.company). V2 key lives in
        ir.config_parameter `lw_shipstation.api_key`.

        Watermark lives in ir.config_parameter
        `lw_shipstation.last_v2_poll` (ISO-8601 UTC string).
        Defaults to 7 days back on first run.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        log_model = self.env['lw.shipstation.log'].sudo()

        api_key = ICP.get_param('lw_shipstation.api_key') or ''
        if not api_key:
            _logger.info(
                'ShipStation V2 poll: ICP lw_shipstation.api_key '
                'unset, skipping.'
            )
            return

        now_utc = datetime.now(timezone.utc)
        watermark_raw = ICP.get_param('lw_shipstation.last_v2_poll')
        if watermark_raw:
            try:
                normalized = watermark_raw
                if normalized.endswith('Z'):
                    normalized = normalized[:-1] + '+00:00'
                watermark = datetime.fromisoformat(normalized)
                if watermark.tzinfo is None:
                    watermark = watermark.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                watermark = now_utc - timedelta(days=7)
        else:
            watermark = now_utc - timedelta(days=7)

        headers = {
            'API-Key': api_key,
            'Accept': 'application/json',
        }
        params = {
            'shipped_after': watermark.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'page_size': 100,
        }

        url = SS_V2_BASE_URL + '/v2/shipments'
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
        except requests.exceptions.RequestException as exc:
            _logger.error('ShipStation V2 poll: HTTP error: %s', exc)
            log_model.create({
                'operation': 'import_tracking',
                'status': 'failed',
                'message': 'V2 poll HTTP error: %s' % str(exc)[:240],
            })
            return

        if resp.status_code != 200:
            _logger.error(
                'ShipStation V2 poll: non-200 response %s: %s',
                resp.status_code, resp.text[:240],
            )
            log_model.create({
                'operation': 'import_tracking',
                'status': 'failed',
                'message': 'V2 poll status %s: %s' % (
                    resp.status_code, resp.text[:200],
                ),
            })
            return

        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            _logger.error('ShipStation V2 poll: malformed JSON in response')
            return

        shipments = data.get('shipments') or []
        updated = 0
        skipped_already_tracked = 0
        unmatched = 0
        Picking = self.env['stock.picking'].sudo()

        for shipment in shipments:
            tracking_number = (shipment.get('tracking_number') or '').strip()
            if not tracking_number:
                continue

            carrier_code = (shipment.get('carrier_code') or '').strip()
            shipment_id = str(shipment.get('shipment_id') or '').strip()
            order_number = (shipment.get('order_number') or '').strip()
            ss_order_id = str(shipment.get('order_id') or '').strip()

            picking = Picking.browse()
            if shipment_id:
                already = Picking.search(
                    [('ss_shipment_id', '=', shipment_id)],
                    limit=1,
                )
                if already:
                    skipped_already_tracked += 1
                    continue

            if ss_order_id:
                picking = Picking.search(
                    [('ss_order_id', '=', ss_order_id)],
                    limit=1,
                )
            if not picking and order_number:
                picking = Picking.search(
                    [('origin', '=', order_number)],
                    limit=1,
                )
            if not picking and order_number:
                picking = Picking.search(
                    [('name', '=', order_number)],
                    limit=1,
                )

            if not picking:
                unmatched += 1
                continue

            if tracking_number in (picking.carrier_tracking_ref or ''):
                skipped_already_tracked += 1
                continue

            existing_ref = picking.carrier_tracking_ref or ''
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

            carrier_label = carrier_code.upper() if carrier_code else 'Unknown'
            picking.message_post(
                body=(
                    'Tracking imported via ShipStation V2 poll '
                    '(source=poll): %s (%s)'
                ) % (tracking_number, carrier_label),
            )

            log_model.create({
                'operation': 'import_tracking',
                'status': 'success',
                'picking_id': picking.id,
                'message': (
                    'V2 poll: %s (%s) shipment_id=%s'
                ) % (tracking_number, carrier_label, shipment_id or '-'),
            })
            updated += 1

        ICP.set_param(
            'lw_shipstation.last_v2_poll',
            now_utc.strftime('%Y-%m-%dT%H:%M:%SZ'),
        )

        _logger.info(
            'ShipStation V2 poll complete: updated=%d, '
            'skipped_already_tracked=%d, unmatched=%d',
            updated, skipped_already_tracked, unmatched,
        )

    def action_refresh_tracking(self):
        """Manual single-picking tracking refresh from ShipStation."""
        self.ensure_one()
        if not self.ss_order_id:
            raise UserError('This picking has not been exported to ShipStation yet.')

        company = self.company_id
        if not company.shipstation_enabled:
            raise UserError('ShipStation integration is not enabled.')

        # Query shipments for this specific order
        params = {
            'orderId': self.ss_order_id,
            'includeShipmentItems': 'true',
        }
        success, result = company._shipstation_api_call(
            'GET', '/shipments', params=params,
        )

        if not success:
            self.env['lw.shipstation.log'].sudo().create({
                'operation': 'import_tracking',
                'status': 'failed',
                'picking_id': self.id,
                'message': f'Refresh failed: {str(result)[:200]}',
            })
            raise UserError(f'Failed to fetch tracking from ShipStation:\n{result[:500]}')

        shipments = result.get('shipments', [])
        if not shipments:
            raise UserError('No shipments found for this order in ShipStation.')

        tracking_numbers = []
        last_shipment_id = ''
        carrier_label = ''
        for shipment in shipments:
            tn = shipment.get('trackingNumber', '')
            if tn:
                tracking_numbers.append(tn)
                last_shipment_id = str(shipment.get('shipmentId', ''))
                carrier_label = (shipment.get('carrierCode', '') or '').upper() or 'Unknown'

        if not tracking_numbers:
            raise UserError('No tracking numbers found for this order in ShipStation.')

        new_ref = ', '.join(tracking_numbers)
        self.write({
            'carrier_tracking_ref': new_ref,
            'ss_shipment_id': last_shipment_id,
            'ss_tracking_synced': True,
        })

        self.message_post(
            body=f'Tracking refreshed: {new_ref} ({carrier_label})',
        )

        self.env['lw.shipstation.log'].sudo().create({
            'operation': 'import_tracking',
            'status': 'success',
            'picking_id': self.id,
            'message': f'Manual refresh: {new_ref} ({carrier_label})',
        })
