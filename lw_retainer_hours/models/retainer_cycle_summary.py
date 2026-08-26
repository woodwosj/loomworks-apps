# Retainer Hours Module
# Copyright (C) 2026 Loomworks LLC
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from odoo import _, api, fields, models


class RetainerCycleSummary(models.Model):
    _name = 'retainer.cycle.summary'
    _description = 'Retainer Cycle Summary'
    _order = 'cycle_start desc, id desc'
    _rec_name = 'display_label'

    plan_id = fields.Many2one(
        'retainer.plan',
        string='Retainer Plan',
        required=True,
        index=True,
        ondelete='cascade',
    )
    partner_id = fields.Many2one(
        related='plan_id.partner_id',
        string='Client',
        store=True,
    )
    display_label = fields.Char(compute='_compute_display_label')
    cycle_start = fields.Date(string='Cycle Start', required=True)
    cycle_end = fields.Date(string='Cycle End', required=True)
    hours_available = fields.Float(string='Available Hours')
    hours_used = fields.Float(string='Used Hours')
    overage_hours = fields.Float(compute='_compute_overage_hours',
                                 string='Overage Hours', store=True)
    rollover_granted = fields.Float(
        string='Rollover Granted To Next Cycle')
    invoice_id = fields.Many2one(
        'account.move',
        string='Renewal Invoice',
        readonly=True,
        copy=False,
    )
    company_id = fields.Many2one(
        related='plan_id.company_id',
        string='Company',
        store=True,
        readonly=True,
    )

    _sql_constraints = [
        ('unique_plan_cycle',
         'unique(plan_id, cycle_start)',
         'A cycle summary already exists for this plan and cycle.'),
    ]

    def _compute_display_label(self):
        for summary in self:
            summary.display_label = '%s: %s → %s' % (
                summary.partner_id.name or '',
                summary.cycle_start or '',
                summary.cycle_end or '',
            )

    @api.depends('hours_available', 'hours_used')
    def _compute_overage_hours(self):
        for summary in self:
            summary.overage_hours = max(
                summary.hours_used - summary.hours_available, 0.0)

    def action_view_invoice(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Renewal Invoice'),
            'res_model': 'account.move',
            'res_id': self.invoice_id.id,
            'view_mode': 'form',
        }
