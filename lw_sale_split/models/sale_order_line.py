from odoo import fields, models, _
from odoo.exceptions import UserError


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    split_target = fields.Integer(
        string="Split Group",
        default=0,
        copy=False,
        help="Set to 1, 2, 3... to assign this line to a new split order. "
             "0 means the line stays on the original order.",
    )

    def action_preview_split(self):
        """Called from the assignment list view header button."""
        # Get the order from the domain context or from selected lines
        order = None
        if self:
            order = self[0].order_id
        else:
            # Try to get from active domain / context
            active_id = self.env.context.get("active_id")
            if active_id:
                order = self.env["sale.order"].browse(active_id)

        if not order:
            # Fallback: get from all lines matching the domain
            domain = self.env.context.get("domain", [])
            if domain:
                lines = self.search(domain, limit=1)
                if lines:
                    order = lines.order_id

        if not order:
            raise UserError(_("Could not determine the sales order."))

        # Check if any lines have split_target > 0
        lines_to_split = order.order_line.filtered(lambda l: l.split_target > 0)
        if not lines_to_split:
            raise UserError(_(
                "No lines have been assigned to a split group yet. "
                "Set the Split Group column to 1, 2, 3, etc."
            ))

        return {
            "type": "ir.actions.act_window",
            "name": _("Preview Split"),
            "res_model": "lw.sale.split.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "active_model": "sale.order",
                "active_id": order.id,
            },
        }
