import logging

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    ss_order_id = fields.Char(
        string='SS Order ID',
        copy=False,
        readonly=True,
    )
    ss_order_key = fields.Char(
        string='SS Order Key',
        copy=False,
        readonly=True,
    )
    ss_exported = fields.Boolean(
        string='Exported to ShipStation',
        default=False,
        copy=False,
        readonly=True,
    )
    ss_export_date = fields.Datetime(
        string='SS Export Date',
        copy=False,
        readonly=True,
    )
    ss_shipment_id = fields.Char(
        string='SS Shipment ID',
        copy=False,
        readonly=True,
    )
    ss_tracking_synced = fields.Boolean(
        string='SS Tracking Synced',
        default=False,
        copy=False,
        readonly=True,
    )

    def _prepare_shipstation_order(self):
        """Build ShipStation order JSON from picking data."""
        self.ensure_one()

        partner = self.partner_id
        if not partner.street or not partner.city:
            raise UserError(
                'Cannot export: shipping address incomplete (missing street or city).'
            )

        def _address_dict(p):
            return {
                'name': p.name or '',
                'street1': p.street or '',
                'street2': p.street2 or '',
                'city': p.city or '',
                'state': p.state_id.code if p.state_id else '',
                'postalCode': p.zip or '',
                'country': p.country_id.code if p.country_id else '',
                'phone': p.phone or '',
            }

        ship_to = _address_dict(partner)

        sale = self.sale_id
        if sale and sale.partner_invoice_id:
            bill_to = _address_dict(sale.partner_invoice_id)
        else:
            bill_to = ship_to

        items = []
        for move in self.move_ids:
            unit_price = 0.0
            if move.sale_line_id:
                unit_price = move.sale_line_id.price_unit
            items.append({
                'sku': move.product_id.default_code or '',
                'name': move.product_id.name,
                'quantity': int(move.product_uom_qty),
                'unitPrice': unit_price,
            })

        order_date = self.scheduled_date
        order_date_str = order_date.isoformat() if order_date else fields.Datetime.now().isoformat()

        # Map Odoo picking state to ShipStation orderStatus
        status_map = {
            'draft': 'on_hold',
            'waiting': 'on_hold',
            'confirmed': 'on_hold',
            'assigned': 'awaiting_shipment',
            'done': 'shipped',
            'cancel': 'cancelled',
        }
        order_status = status_map.get(self.state, 'awaiting_shipment')

        payload = {
            'orderNumber': self.origin or self.name,
            'orderKey': self.name,
            'orderDate': order_date_str,
            'orderStatus': order_status,
            'shipTo': ship_to,
            'billTo': bill_to,
            'items': items,
        }

        store_id = self.company_id.shipstation_store_id
        if store_id:
            try:
                payload['advancedOptions'] = {'storeId': int(store_id)}
            except ValueError:
                _logger.warning('Invalid ShipStation store ID: %s (not numeric, skipping)', store_id)

        return payload

    def action_open_shipstation(self):
        """Open ShipStation's Awaiting Shipment queue in a new browser tab.

        The legacy per-order deep link (app.shipstation.com/orders/<id>) no
        longer resolves: ShipStation migrated accounts to regional pods
        (ship<N>.shipstation.com) and order-detail URLs now use ShipStation's
        internal IDs, not the numeric ss_order_id we store. We instead open
        the company's configured ShipStation web base at the Awaiting
        Shipment queue, where the operator can search by order number.
        """
        self.ensure_one()
        if not self.ss_order_id:
            raise UserError(
                'This picking has not been linked to a ShipStation order yet.'
            )
        base = (self.company_id.shipstation_web_url
                or 'https://ship14.shipstation.com').rstrip('/')
        return {
            'type': 'ir.actions.act_url',
            'url': '%s/orders/awaiting-shipment' % base,
            'target': 'new',
        }

    def action_export_to_shipstation(self):
        """Export this picking to ShipStation."""
        self.ensure_one()

        if self.ss_exported:
            raise UserError('This picking has already been exported to ShipStation.')

        if not self.company_id.shipstation_enabled:
            raise UserError('ShipStation integration is not enabled.')

        if (self.picking_type_id.code != 'outgoing'
                or self.location_dest_id.usage != 'customer'):
            raise UserError(
                'Only outgoing customer deliveries can be exported to ShipStation.'
            )

        payload = self._prepare_shipstation_order()
        success, result = self.company_id._shipstation_api_call(
            'POST', '/orders/createorder', data=payload
        )

        if success:
            ss_order_id = str(result.get('orderId', ''))
            ss_order_key = result.get('orderKey', '')
            self.write({
                'ss_order_id': ss_order_id,
                'ss_order_key': ss_order_key,
                'ss_exported': True,
                'ss_export_date': fields.Datetime.now(),
            })
            self.message_post(body=f'Exported to ShipStation (Order #{ss_order_id})')
            self.env['lw.shipstation.log'].sudo().create({
                'operation': 'export_order',
                'status': 'success',
                'picking_id': self.id,
                'request_data': str(payload)[:5000],
                'response_data': str(result)[:5000],
                'message': f'Order {ss_order_id} created',
            })
        else:
            self.env['lw.shipstation.log'].sudo().create({
                'operation': 'export_order',
                'status': 'failed',
                'picking_id': self.id,
                'request_data': str(payload)[:5000],
                'response_data': str(result)[:5000],
                'message': f'Export failed: {result[:200]}',
            })
            raise UserError(f'ShipStation export failed:\n{result[:500]}')

    def write(self, vals):
        res = super().write(vals)
        if vals.get('state') == 'assigned':
            for picking in self:
                if (picking.company_id.shipstation_enabled
                        and picking.company_id.shipstation_auto_export
                        and not picking.ss_exported
                        and picking.picking_type_id.code == 'outgoing'
                        and picking.location_dest_id.usage == 'customer'):
                    try:
                        picking.action_export_to_shipstation()
                    except Exception as e:
                        _logger.warning(
                            'Auto-export to ShipStation failed for %s: %s',
                            picking.name, e,
                        )
        return res

