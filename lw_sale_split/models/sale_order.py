from odoo import models, _


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def action_open_split_assign(self):
        """Open the split group assignment list view."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Assign Split Groups — %s") % self.name,
            "res_model": "sale.order.line",
            "view_mode": "list",
            "views": [
                (self.env.ref("lw_sale_split.view_sale_order_line_split_assign").id, "list"),
            ],
            "search_view_id": [
                self.env.ref("lw_sale_split.view_sale_order_line_split_search").id,
            ],
            "domain": [("order_id", "=", self.id)],
            "target": "current",
        }
