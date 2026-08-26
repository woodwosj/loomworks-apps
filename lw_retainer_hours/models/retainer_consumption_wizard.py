# Retainer Hours Module
# Copyright (C) 2026 Loomworks LLC
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

from odoo import fields, models


class RetainerConsumptionWizard(models.TransientModel):
    _name = 'retainer.consumption.wizard'
    _description = 'Retainer Consumption Wizard'

    plan_id = fields.Many2one(
        'retainer.plan',
        string='Retainer Plan',
        required=True,
    )
    date = fields.Date(
        string='Date',
        default=fields.Date.context_today,
        required=True,
    )
    hours = fields.Float(string='Hours', required=True)
    description = fields.Char(string='Description')

    def action_confirm(self):
        self.ensure_one()
        self.env['retainer.consumption'].create({
            'plan_id': self.plan_id.id,
            'date': self.date,
            'hours': self.hours,
            'description': self.description,
            'source': 'manual',
        })
