# Retainer Hours Module
# Copyright (C) 2026 Loomworks LLC
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0)

import calendar

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class RetainerPlan(models.Model):
    _name = 'retainer.plan'
    _description = 'Retainer Plan'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(
        string='Reference',
        required=True,
        default=lambda self: _('New'),
        copy=False,
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Client',
        required=True,
        tracking=True,
        index=True,
    )
    product_id = fields.Many2one(
        'product.product',
        string='Retainer Product',
        default=lambda self: self._default_retainer_product(),
        tracking=True,
    )
    monthly_hours = fields.Float(
        string='Included Hours / Cycle',
        default=10.0,
        tracking=True,
    )
    in_plan_rate = fields.Monetary(
        string='In-plan Rate',
        default=40.0,
        currency_field='currency_id',
    )
    overage_rate = fields.Monetary(
        string='Overage Rate',
        default=60.0,
        currency_field='currency_id',
    )
    rollover_policy = fields.Selection(
        [('one_cycle', 'Roll over one cycle'),
         ('none', 'No rollover')],
        string='Rollover Policy',
        default='one_cycle',
        required=True,
        tracking=True,
    )
    cycle_start_day = fields.Integer(
        string='Cycle Start Day',
        default=28,
        required=True,
        tracking=True,
    )
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        default=lambda self: self.env.company,
        required=True,
        index=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id',
        string='Currency',
        readonly=True,
        store=True,
    )

    consumption_line_ids = fields.One2many(
        'retainer.consumption',
        'plan_id',
        string='Consumptions',
    )
    cycle_summary_ids = fields.One2many(
        'retainer.cycle.summary',
        'plan_id',
        string='Cycle Summaries',
    )

    # -- cycle boundaries / live usage ------------------------------------
    current_cycle_start = fields.Date(
        string='Current Cycle Start',
        compute='_compute_cycle_fields',
        store=True,
    )
    current_cycle_end = fields.Date(
        string='Current Cycle End',
        compute='_compute_cycle_fields',
        store=True,
    )
    rolled_over_hours = fields.Float(
        string='Rolled Over Into This Cycle',
        compute='_compute_cycle_fields',
        store=True,
        help='Hours carried over from the previous cycle (one-cycle policy only).',
    )
    hours_available_this_cycle = fields.Float(
        string='Available Hours',
        compute='_compute_cycle_fields',
        store=True,
        help='Included hours plus the rollover granted into this cycle.',
    )
    hours_used_this_cycle = fields.Float(
        string='Used Hours',
        compute='_compute_cycle_fields',
        store=True,
    )
    hours_remaining = fields.Float(
        string='Remaining Hours',
        compute='_compute_cycle_fields',
        store=True,
        help='Negative when the client is in overage.',
    )
    overage_hours = fields.Float(
        string='Overage Hours',
        compute='_compute_cycle_fields',
        store=True,
    )
    rollover_next_cycle = fields.Float(
        string='Rollover To Next Cycle',
        compute='_compute_cycle_fields',
        store=True,
        help='Surplus hours granted into the next cycle, capped at the '
             'monthly allowance. Unused granted hours are never banked '
             'for a second cycle.',
    )

    # -- defaults ----------------------------------------------------------
    @api.model
    def _default_retainer_product(self):
        """Default to the Loomworks Care Retainer (RET-CARE) when present."""
        return self.env['product.product'].search(
            [('default_code', '=', 'RET-CARE')], limit=1)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                seq = self.env['ir.sequence'].next_by_code('retainer.plan')
                vals['name'] = seq or _('New')
        return super().create(vals_list)

    @api.constrains('cycle_start_day')
    def _check_cycle_start_day(self):
        for plan in self:
            if not 1 <= plan.cycle_start_day <= 31:
                raise UserError(_('Cycle start day must be between 1 and 31.'))

    # -- cycle math ---------------------------------------------------------
    def _add_months(self, ref_date, months):
        """Month arithmetic that clamps the day to the target month length."""
        month_index = ref_date.month - 1 + months
        year = ref_date.year + month_index // 12
        month = month_index % 12 + 1
        day = min(ref_date.day, calendar.monthrange(year, month)[1])
        return ref_date.replace(year=year, month=month, day=day)

    def _get_cycle_bounds(self, ref_date):
        """Return (start, end) dates of the cycle containing ref_date."""
        self.ensure_one()
        day = min(self.cycle_start_day, 31)
        if ref_date.day >= day:
            start = ref_date.replace(day=day)
        else:
            first_of_month = ref_date.replace(day=1)
            last_of_prev = first_of_month - relativedelta(days=1)
            start = last_of_prev.replace(day=min(day, last_of_prev.day))
        end = self._add_months(start, 1) - relativedelta(days=1)
        return start, end

    def _get_cycle_usage(self, cycle_start, cycle_end):
        """Sum of consumption hours logged inside [cycle_start, cycle_end]."""
        self.ensure_one()
        self.env.cr.execute(
            """
            SELECT COALESCE(SUM(hours), 0.0)
            FROM retainer_consumption
            WHERE plan_id = %s
              AND date >= %s
              AND date <= %s
            """,
            (self.id, cycle_start, cycle_end),
        )
        return self.env.cr.fetchone()[0]

    def _rollover_granted_for(self, cycle_start):
        """Hours rolled over INTO the cycle starting at cycle_start.

        One-cycle policy only: the surplus of the immediately preceding
        cycle, measured against the monthly allowance (not against its
        total available hours, so granted hours are never banked twice),
        capped at monthly_hours.
        """
        self.ensure_one()
        if self.rollover_policy != 'one_cycle':
            return 0.0
        prev_start, prev_end = self._get_cycle_bounds(
            cycle_start - relativedelta(days=1))
        prev_used = self._get_cycle_usage(prev_start, prev_end)
        return min(max(self.monthly_hours - prev_used, 0.0),
                   self.monthly_hours)

    @api.depends('cycle_start_day', 'monthly_hours', 'rollover_policy',
                 'consumption_line_ids.date', 'consumption_line_ids.hours')
    def _compute_cycle_fields(self):
        today = fields.Date.context_today(self)
        for plan in self:
            start, end = plan._get_cycle_bounds(today)
            used = plan._get_cycle_usage(start, end)
            rollover = plan._rollover_granted_for(start)
            available = plan.monthly_hours + rollover
            plan.current_cycle_start = start
            plan.current_cycle_end = end
            plan.rolled_over_hours = rollover
            plan.hours_available_this_cycle = available
            plan.hours_used_this_cycle = used
            plan.hours_remaining = available - used
            plan.overage_hours = max(used - available, 0.0)
            if plan.rollover_policy == 'one_cycle':
                plan.rollover_next_cycle = min(
                    max(plan.monthly_hours - used, 0.0), plan.monthly_hours)
            else:
                plan.rollover_next_cycle = 0.0

    # -- UI actions ----------------------------------------------------------
    def action_add_consumption(self):
        self.ensure_one()
        return {
            'name': _('Add Consumption'),
            'type': 'ir.actions.act_window',
            'res_model': 'retainer.consumption.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_plan_id': self.id},
        }

    def action_view_cycle_summaries(self):
        self.ensure_one()
        return {
            'name': _('Cycle Summaries'),
            'type': 'ir.actions.act_window',
            'res_model': 'retainer.cycle.summary',
            'view_mode': 'list,form',
            'domain': [('plan_id', '=', self.id)],
        }

    # -- cron ----------------------------------------------------------------
    def _cron_cycle_rollover(self):
        """Daily job: close cycles that ended in the last 24h.

        For each such plan, snapshot usage into retainer.cycle.summary and
        draft the renewal invoice (retainer product line + overage line
        when applicable). Idempotent: a plan whose cycle already has a
        summary is skipped entirely.
        """
        today = fields.Date.context_today(self)
        summary_model = self.env['retainer.cycle.summary'].sudo()
        for plan in self.search([]):
            cur_start, _dummy = plan._get_cycle_bounds(today)
            ended_end = cur_start - relativedelta(days=1)
            ended_start, _dummy = plan._get_cycle_bounds(ended_end)
            if (today - ended_end).days > 1:
                continue
            if summary_model.search_count([
                    ('plan_id', '=', plan.id),
                    ('cycle_start', '=', ended_start)]):
                continue
            used = plan._get_cycle_usage(ended_start, ended_end)
            rollover_in = plan._rollover_granted_for(ended_start)
            available = plan.monthly_hours + rollover_in
            overage = max(used - available, 0.0)
            if plan.rollover_policy == 'one_cycle':
                granted = min(max(plan.monthly_hours - used, 0.0),
                              plan.monthly_hours)
            else:
                granted = 0.0
            summary = summary_model.create({
                'plan_id': plan.id,
                'cycle_start': ended_start,
                'cycle_end': ended_end,
                'hours_available': available,
                'hours_used': used,
                'rollover_granted': granted,
            })
            plan.sudo()._create_renewal_invoice(summary, overage)

    def _create_renewal_invoice(self, summary, overage_hours):
        """Draft the renewal invoice for a closed cycle on this plan."""
        self.ensure_one()
        journal = self.env['account.journal'].sudo().search(
            [('type', '=', 'sale'), ('company_id', '=', self.company_id.id)],
            limit=1)
        if not journal:
            return False
        lines = [(0, 0, {
            'product_id': self.product_id.id,
            'name': self.product_id.display_name,
            'quantity': 1,
            'price_unit': self.product_id.list_price,
        })]
        if overage_hours > 0:
            lines.append((0, 0, {
                'name': _('Retainer overage %.2fh', overage_hours),
                'quantity': overage_hours,
                'price_unit': self.overage_rate,
            }))
        invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_id.id,
            'journal_id': journal.id,
            'invoice_line_ids': lines,
        })
        summary.invoice_id = invoice.id
        return invoice
