# Retainer Hours Module
# Copyright (C) 2026 Loomworks Solutions LLC
# License OPL-1 (https://www.odoo.com/documentation/master/legal/licenses.html)

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestRetainerDemo(TransactionCase):
    """Demo acceptance case: $400/mo plan, 10 included hours, cycle resets
    on the 28th, one-cycle rollover, $40/hr in-plan, $60/hr overage.
    """

    def _create_plan(self, **overrides):
        partner = self.env['res.partner'].create({'name': 'Demo Vans'})
        product = self.env['product.product'].create({
            'name': 'Loomworks Care Retainer',
            'default_code': 'RET-CARE',
            'list_price': 400.0,
            'type': 'service',
        })
        vals = {
            'partner_id': partner.id,
            'product_id': product.id,
            'monthly_hours': 10.0,
            'in_plan_rate': 40.0,
            'overage_rate': 60.0,
            'rollover_policy': 'one_cycle',
            'cycle_start_day': 28,
        }
        vals.update(overrides)
        return self.env['retainer.plan'].create(vals)

    def _log(self, plan, date, hours, description='Work'):
        return self.env['retainer.consumption'].create({
            'plan_id': plan.id,
            'date': date,
            'hours': hours,
            'description': description,
            'source': 'manual',
        })

    def test_demo_rollover_and_overage(self):
        """Cycle A: use 5.25h of 10 -> 4.75 rolls over.
        Cycle B: starts with 14.75h available; use 16h -> 1.25h overage.
        """
        plan = self._create_plan()
        today = fields.Date.context_today(plan)

        # A date guaranteed to fall inside the previous cycle.
        prev_cycle_date = plan.current_cycle_start - relativedelta(days=10)
        self._log(plan, prev_cycle_date, 5.25, 'Cycle A work')

        # Current cycle: nothing used yet, 4.75h rolled over from cycle A.
        self.assertAlmostEqual(plan.hours_used_this_cycle, 0.0)
        self.assertAlmostEqual(plan.rolled_over_hours, 4.75)
        self.assertAlmostEqual(plan.hours_available_this_cycle, 14.75)
        self.assertAlmostEqual(plan.rollover_next_cycle, 10.0)

        # Cycle B usage: 16h against 14.75h available.
        self._log(plan, today, 16.0, 'Cycle B work')
        self.assertAlmostEqual(plan.hours_used_this_cycle, 16.0)
        self.assertAlmostEqual(plan.hours_available_this_cycle, 14.75)
        self.assertAlmostEqual(plan.hours_remaining, -1.25)
        self.assertAlmostEqual(plan.overage_hours, 1.25)
        # Overage billed at the overage rate.
        self.assertAlmostEqual(plan.overage_hours * plan.overage_rate, 75.0)
        # Used more than the allowance: no rollover to the next cycle.
        self.assertAlmostEqual(plan.rollover_next_cycle, 0.0)

    def test_no_banking(self):
        """Granted rollover hours do not roll over a second time."""
        plan = self._create_plan()
        # Empty cycles before the previous one would each grant a full
        # rollover; only the immediately preceding cycle's surplus counts.
        prev_cycle_date = plan.current_cycle_start - relativedelta(days=10)
        self._log(plan, prev_cycle_date, 10.0, 'Exactly the allowance')
        self.assertAlmostEqual(plan.rolled_over_hours, 0.0)
        self.assertAlmostEqual(plan.hours_available_this_cycle, 10.0)

    def test_no_rollover_policy(self):
        plan = self._create_plan(rollover_policy='none')
        today = fields.Date.context_today(plan)
        prev_cycle_date = plan.current_cycle_start - relativedelta(days=10)
        self._log(plan, prev_cycle_date, 3.0, 'Spare hours')
        self._log(plan, today, 2.0, 'Current work')
        self.assertAlmostEqual(plan.rolled_over_hours, 0.0)
        self.assertAlmostEqual(plan.hours_available_this_cycle, 10.0)
        self.assertAlmostEqual(plan.hours_used_this_cycle, 2.0)

    def test_consumption_hours_must_be_positive(self):
        plan = self._create_plan()
        with self.assertRaises(Exception):
            self._log(plan, fields.Date.context_today(plan), 0.0)

    def test_cron_idempotent(self):
        """Running the rollover cron twice yields one summary and one
        renewal invoice for the cycle that just ended.
        """
        today = fields.Date.context_today(self.env['retainer.plan'])
        plan = self._create_plan(cycle_start_day=today.day)
        # Yesterday belongs to the cycle that just ended.
        self._log(plan, today - relativedelta(days=1), 3.0, 'Final cycle work')

        plan._cron_cycle_rollover()
        plan._cron_cycle_rollover()

        summaries = self.env['retainer.cycle.summary'].search(
            [('plan_id', '=', plan.id)])
        self.assertEqual(len(summaries), 1)
        self.assertAlmostEqual(summaries.hours_used, 3.0)

        invoices = self.env['account.move'].search(
            [('partner_id', '=', plan.partner_id.id),
             ('move_type', '=', 'out_invoice')])
        self.assertEqual(len(invoices), 1)
        self.assertEqual(summaries.invoice_id.id, invoices.id)
        # Retainer line at the product price + no overage line (3h < 10h).
        retainer_line = invoices.invoice_line_ids.filtered(
            lambda line: line.product_id == plan.product_id)
        self.assertAlmostEqual(retainer_line.price_unit, 400.0)
        self.assertFalse(invoices.invoice_line_ids.filtered(
            lambda line: 'overage' in (line.name or '').lower()))
