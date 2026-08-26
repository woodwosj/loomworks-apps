# Retainer Hours Module
# Copyright (C) 2026 Loomworks LLC
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class RetainerConsumption(models.Model):
    _name = 'retainer.consumption'
    _description = 'Retainer Hours Consumption'
    _order = 'date desc, id desc'

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
    timesheet_line_id = fields.Many2one(
        'account.analytic.line',
        string='Timesheet Line',
        index=True,
    )
    date = fields.Date(
        string='Date',
        default=fields.Date.context_today,
        required=True,
    )
    hours = fields.Float(string='Hours', required=True)
    description = fields.Char(string='Description')
    source = fields.Selection(
        [('timesheet', 'Timesheet'), ('manual', 'Manual entry')],
        string='Source',
        default='manual',
        required=True,
    )
    company_id = fields.Many2one(
        related='plan_id.company_id',
        string='Company',
        store=True,
        readonly=True,
    )

    @api.constrains('hours')
    def _check_hours_positive(self):
        for consumption in self:
            if consumption.hours <= 0:
                raise ValidationError(_('Consumed hours must be positive.'))
