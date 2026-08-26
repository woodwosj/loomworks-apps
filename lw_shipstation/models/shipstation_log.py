from odoo import fields, models


class ShipStationLog(models.Model):
    _name = 'lw.shipstation.log'
    _description = 'ShipStation Operation Log'
    _order = 'create_date desc'

    operation = fields.Selection(
        selection=[
            ('export_order', 'Export Order'),
            ('import_tracking', 'Import Tracking'),
            ('import_orders', 'Import Orders'),
            ('test_connection', 'Test Connection'),
            ('error', 'Error'),
        ],
        string='Operation',
        required=True,
    )
    status = fields.Selection(
        selection=[
            ('success', 'Success'),
            ('failed', 'Failed'),
        ],
        string='Status',
        required=True,
    )
    picking_id = fields.Many2one(
        'stock.picking',
        string='Picking',
        ondelete='set null',
    )
    request_data = fields.Text(string='Request Data')
    response_data = fields.Text(string='Response Data')
    message = fields.Char(string='Message')
