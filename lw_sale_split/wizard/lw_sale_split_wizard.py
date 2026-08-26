from odoo import api, fields, models, _
from odoo.exceptions import UserError
from markupsafe import Markup


class SaleOrderSplitWizard(models.TransientModel):
    _name = "lw.sale.split.wizard"
    _description = "Split Sale Order Wizard"

    sale_order_id = fields.Many2one(
        "sale.order",
        string="Sales Order",
        required=True,
        readonly=True,
    )
    summary_text = fields.Html(
        string="Split Summary",
        readonly=True,
    )

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        active_model = self.env.context.get("active_model")
        active_id = self.env.context.get("active_id")
        if active_model == "sale.order" and active_id:
            order = self.env["sale.order"].browse(active_id)
            vals["sale_order_id"] = order.id
            vals["summary_text"] = self._build_summary_html(order)
        return vals

    def _get_base_order_name(self, order):
        name = order.name or ""
        parts = name.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            return parts[0]
        return name

    def _get_next_split_number(self, base_name):
        existing = self.env["sale.order"].search([
            ("name", "=like", base_name + "-%"),
        ])
        max_num = 0
        for order in existing:
            parts = order.name.rsplit("-", 1)
            if len(parts) == 2 and parts[1].isdigit():
                max_num = max(max_num, int(parts[1]))
        return max_num + 1

    def _order_link(self, order):
        """Build an HTML link to a sale order for chatter messages."""
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        url = "%s/odoo/sales/%d" % (base_url, order.id)
        return Markup('<a href="%s">%s</a>') % (url, order.name)

    def _build_summary_html(self, order):
        base_name = self._get_base_order_name(order)
        next_num = self._get_next_split_number(base_name)

        groups = {}
        remaining = []
        for line in order.order_line:
            if line.split_target and line.split_target > 0:
                groups.setdefault(line.split_target, []).append(line)
            else:
                remaining.append(line)

        if not groups:
            return "<p>No lines have been assigned to a split group.</p>"

        table_style = (
            "width:100%; border-collapse:collapse; margin-bottom:16px;"
        )
        th_style = (
            "text-align:left; padding:8px 10px; "
            "border-bottom:2px solid currentColor; opacity:0.7; font-size:13px;"
        )
        td_style = (
            "padding:8px 10px; border-bottom:1px solid rgba(128,128,128,0.3);"
        )
        td_right = td_style + " text-align:right;"
        section_style = (
            "margin:16px 0 8px 0; padding:8px 0; font-size:15px; font-weight:600; "
            "border-bottom:2px solid rgba(128,128,128,0.4);"
        )

        # Warning if confirmed with partial deliveries
        warning = ""
        if order.state == "sale":
            has_delivered = any(
                line.qty_delivered > 0
                for line in order.order_line
                if line.split_target and line.split_target > 0
            )
            if has_delivered:
                warning = (
                    '<div style="background:rgba(255,165,0,0.15); border:1px solid orange; '
                    'padding:10px; margin-bottom:12px; border-radius:4px;">'
                    '⚠️ <b>Warning:</b> Some lines being split have delivered quantities. '
                    'Only the remaining undelivered quantities will be moved to the new order(s). '
                    'Delivered quantities will stay on the original order.</div>'
                )

        parts = [warning] if warning else []

        # Status info
        if order.state == "sale":
            parts.append(
                '<div style="padding:6px 10px; margin-bottom:12px; '
                'background:rgba(100,149,237,0.15); border-radius:4px;">'
                'ℹ️ This is a <b>confirmed</b> order. New order(s) will be '
                'auto-confirmed and delivery orders will be updated automatically.</div>'
            )

        # Remaining on original
        if remaining:
            parts.append(
                '<div style="%s">📋 Staying on <b>%s</b> (%d lines)</div>'
                % (section_style, order.name, len(remaining))
            )
            parts.append('<table style="%s">' % table_style)
            parts.append(
                '<tr><th style="%s">Product</th>'
                '<th style="%s text-align:right;">Qty</th>'
                '<th style="%s">Unit</th>'
                '<th style="%s text-align:right;">Price</th></tr>'
                % (th_style, th_style, th_style, th_style)
            )
            for line in remaining:
                product_name = line.product_id.display_name if line.product_id else (line.name or "")
                parts.append(
                    '<tr><td style="%s">%s</td>'
                    '<td style="%s">%s</td>'
                    '<td style="%s">%s</td>'
                    '<td style="%s">%s</td></tr>'
                    % (
                        td_style, product_name,
                        td_right, line.product_uom_qty,
                        td_style, line.product_uom_id.name if line.product_uom_id else "",
                        td_right, line.price_unit,
                    )
                )
            parts.append("</table>")

        # Each split group
        for group_key in sorted(groups.keys()):
            group_lines = groups[group_key]
            split_name = "%s-%d" % (base_name, next_num)
            parts.append(
                '<div style="%s">✂️ New Order: <b>%s</b> (Group %d — %d lines)</div>'
                % (section_style, split_name, group_key, len(group_lines))
            )
            parts.append('<table style="%s">' % table_style)
            parts.append(
                '<tr><th style="%s">Product</th>'
                '<th style="%s text-align:right;">Qty</th>'
                '<th style="%s">Unit</th>'
                '<th style="%s text-align:right;">Price</th></tr>'
                % (th_style, th_style, th_style, th_style)
            )
            for line in group_lines:
                product_name = line.product_id.display_name if line.product_id else (line.name or "")
                parts.append(
                    '<tr><td style="%s">%s</td>'
                    '<td style="%s">%s</td>'
                    '<td style="%s">%s</td>'
                    '<td style="%s">%s</td></tr>'
                    % (
                        td_style, product_name,
                        td_right, line.product_uom_qty,
                        td_style, line.product_uom_id.name if line.product_uom_id else "",
                        td_right, line.price_unit,
                    )
                )
            parts.append("</table>")
            next_num += 1

        return Markup("".join(parts))

    def _nuke_moves_for_line(self, line):
        """Cancel and fully remove stock moves linked to a sale order line."""
        moves = self.env["stock.move"].search([
            ("sale_line_id", "=", line.id),
            ("state", "not in", ("done",)),
        ])
        if moves:
            # Cancel first
            to_cancel = moves.filtered(lambda m: m.state != "cancel")
            if to_cancel:
                to_cancel._action_cancel()
            # Remove move lines (detailed operations)
            moves.move_line_ids.sudo().unlink()
            # Now unlink the moves themselves
            moves.sudo().with_context(force_delete=True).unlink()

    def _remove_line_from_confirmed_order(self, line, dest_order_name=None):
        """Remove a line from a confirmed SO: nuke moves, then zero out the line.

        Odoo 19 prevents deletion of confirmed SO lines to preserve audit
        trails, so we cancel/remove pending stock moves and set the line
        quantity to zero instead.  A note is appended to the line description
        so reviewers know where the product went.
        """
        # 1. Cancel and remove non-done stock moves
        self._nuke_moves_for_line(line)
        # 2. Zero out the line (Odoo 19 blocks unlink on confirmed orders)
        zero_vals = {
            "product_uom_qty": 0,
            "split_target": 0,
        }
        # Always zero bolt_qty if the field exists on the line
        if hasattr(line, 'bolt_qty'):
            zero_vals["bolt_qty"] = 0
        # 3. Annotate the line description so reviewers know where it went
        if dest_order_name:
            note = "\n⤷ Moved to %s" % dest_order_name
            zero_vals["name"] = (line.name or "") + note
        line.write(zero_vals)

    def _cleanup_empty_pickings(self, order):
        """Cancel any pickings that have no remaining moves after split."""
        for picking in order.picking_ids:
            if picking.state in ("done", "cancel"):
                continue
            active_moves = picking.move_ids.filtered(
                lambda m: m.state not in ("done", "cancel")
            )
            if not active_moves:
                picking.action_cancel()

    def action_split(self):
        self.ensure_one()
        order = self.sale_order_id
        is_confirmed = order.state == "sale"

        groups = {}
        for line in order.order_line:
            if line.split_target and line.split_target > 0:
                groups.setdefault(line.split_target, self.env["sale.order.line"])
                groups[line.split_target] |= line

        if not groups:
            raise UserError(_("No lines have been assigned to a split group."))

        # Block if any line being split has been invoiced
        for lines in groups.values():
            for line in lines:
                if line.qty_invoiced > 0:
                    raise UserError(_(
                        "Line '%s' has already been invoiced (qty invoiced: %s) "
                        "and cannot be split. Remove it from the split group (set to 0)."
                    ) % (line.product_id.display_name or line.name, line.qty_invoiced))

        # Block if any line being split is fully delivered
        if is_confirmed:
            for lines in groups.values():
                for line in lines:
                    if line.qty_delivered >= line.product_uom_qty and line.qty_delivered > 0:
                        raise UserError(_(
                            "Line '%s' is fully delivered and cannot be split. "
                            "Remove it from the split group (set to 0)."
                        ) % (line.product_id.display_name or line.name))

        base_name = self._get_base_order_name(order)
        next_num = self._get_next_split_number(base_name)
        new_orders = self.env["sale.order"]
        split_details = []

        for group_key in sorted(groups.keys()):
            lines = groups[group_key]
            split_name = "%s-%d" % (base_name, next_num)

            # Use copy() to preserve all fields (warehouse_id, team_id, etc.)
            new_order = order.copy({"order_line": False, "origin": order.name})

            # Set name BEFORE confirm so pickings get the correct source document
            new_order.name = split_name
            new_order.date_order = order.date_order

            line_details = []
            for line in lines:
                # Calculate the remaining (undelivered) quantity to move
                remaining_qty = line.product_uom_qty - line.qty_delivered
                if remaining_qty <= 0:
                    continue

                copy_vals = {
                    "order_id": new_order.id,
                    "split_target": 0,
                    "product_uom_qty": remaining_qty,
                }
                # If this is a bolt product, recalculate bolt_qty for the remaining
                if hasattr(line, 'is_fabric_bolt_line') and line.is_fabric_bolt_line:
                    if line.yards_per_bolt and line.yards_per_bolt > 0:
                        copy_vals["bolt_qty"] = remaining_qty / line.yards_per_bolt
                    elif line.bolt_qty and line.product_uom_qty:
                        # Proportional fallback
                        copy_vals["bolt_qty"] = line.bolt_qty * (remaining_qty / line.product_uom_qty)

                new_line = line.copy(copy_vals)

                line_details.append(
                    "%s (Qty: %s)" % (
                        line.product_id.display_name or line.name,
                        remaining_qty,
                    )
                )

                # Handle the original line based on whether it has done deliveries
                done_moves = self.env["stock.move"].search([
                    ("sale_line_id", "=", line.id),
                    ("state", "=", "done"),
                ])
                if done_moves and line.qty_delivered > 0:
                    # Partial delivery: keep the line for traceability but reduce
                    # to delivered qty and annotate
                    if is_confirmed:
                        self._nuke_moves_for_line(line)
                        reduce_vals = {
                            "product_uom_qty": line.qty_delivered,
                            "split_target": 0,
                            "name": (line.name or "") + "\n⤷ Remaining qty moved to %s" % split_name,
                        }
                        if hasattr(line, 'is_fabric_bolt_line') and line.is_fabric_bolt_line:
                            if line.yards_per_bolt and line.yards_per_bolt > 0:
                                reduce_vals["bolt_qty"] = line.qty_delivered / line.yards_per_bolt
                        line.write(reduce_vals)
                    else:
                        line.unlink()
                else:
                    # No done moves — safe to zero out (confirmed) or delete (draft)
                    if is_confirmed:
                        self._remove_line_from_confirmed_order(line, dest_order_name=split_name)
                    else:
                        line.unlink()

            # Auto-confirm the new order if original was confirmed
            if is_confirmed:
                new_order.action_confirm()
                # Re-set name in case action_confirm overwrote it with a new sequence
                new_order.name = split_name
                # Ensure pickings reference the correct source document
                for picking in new_order.picking_ids:
                    if picking.origin != split_name:
                        picking.origin = split_name

            # Post chatter on the NEW order with link to parent
            order_link = self._order_link(order)
            new_order.message_post(
                body=Markup(
                    "<p>✂️ This order was created by splitting from %s.</p>"
                    "<p>Lines moved:</p><ul>%s</ul>"
                ) % (
                    order_link,
                    Markup("").join(
                        Markup("<li>%s</li>") % d for d in line_details
                    ),
                ),
                subtype_xmlid="mail.mt_note",
            )

            split_details.append((new_order, line_details))
            new_orders |= new_order
            next_num += 1

        # Reset any remaining lines
        remaining = order.order_line.filtered(lambda l: l.exists())
        if remaining:
            remaining.write({"split_target": 0})

        # Clean up empty pickings on original order
        if is_confirmed:
            self._cleanup_empty_pickings(order)

        # Post chatter on the ORIGINAL order with links to new orders
        split_summary_parts = []
        for new_ord, slines in split_details:
            new_link = self._order_link(new_ord)
            split_summary_parts.append(
                Markup("<li>%s:<ul>%s</ul></li>") % (
                    new_link,
                    Markup("").join(
                        Markup("<li>%s</li>") % d for d in slines
                    ),
                )
            )
        order.message_post(
            body=Markup(
                "<p>✂️ This order was split. The following lines were moved "
                "to new orders:</p><ul>%s</ul>"
            ) % Markup("").join(split_summary_parts),
            subtype_xmlid="mail.mt_note",
        )

        if len(new_orders) == 1:
            return {
                "type": "ir.actions.act_window",
                "res_model": "sale.order",
                "view_mode": "form",
                "res_id": new_orders.id,
            }

        return {
            "type": "ir.actions.act_window",
            "name": _("Split Orders"),
            "res_model": "sale.order",
            "view_mode": "list,form",
            "domain": [("id", "in", new_orders.ids)],
        }
