from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    def action_shipstation_test_connection(self):
        """Proxy to company method for settings form button."""
        self.env.company.action_shipstation_test_connection()

    shipstation_api_key = fields.Char(
        related='company_id.shipstation_api_key',
        readonly=False,
    )
    shipstation_api_secret = fields.Char(
        related='company_id.shipstation_api_secret',
        readonly=False,
    )
    shipstation_enabled = fields.Boolean(
        related='company_id.shipstation_enabled',
        readonly=False,
    )
    shipstation_auto_export = fields.Boolean(
        related='company_id.shipstation_auto_export',
        readonly=False,
    )
    shipstation_store_id = fields.Char(
        related='company_id.shipstation_store_id',
        readonly=False,
    )
    shipstation_web_url = fields.Char(
        related='company_id.shipstation_web_url',
        readonly=False,
    )
