# -*- coding: utf-8 -*-
"""Tests for the service charge run records.

The run record is the report the accounting team asked for: for every cron tick, which
invoices accrued the most interest, and for every customer who was not
charged, why not. These tests pin the four properties that make it
trustworthy:

  1. A dry run writes the report and creates ZERO accounting objects.
     Run records are audit objects; account.move rows are not.
  2. Suppression is visible. A production census found
     suppression to be the dominant real-world path, so a report that
     only listed charges would explain almost nothing.
  3. Live mode writes BOTH the report and the charge invoice.
  4. The breakdown is per invoice, not per partner -- otherwise the
     question "which invoices have the most interest accrued" has no
     answer in the shipping (separate_invoice) mode.

Plus the seam that this design exists to protect: a partner whose
processing blows up inside its savepoint must still appear in the
report. The audit row is contributed from OUTSIDE the savepoint
precisely so the rollback cannot erase it.

These run on a database that may carry production data. Every assertion
is scoped to this test's own partner (``_lines_for``): a run on the
staging clone legitimately contains a suppression line for every
pre-existing past-due debtor, all of them exempted by the shared
fixture, and a global count assertion would be measuring that instead.

Test class follows the TransactionCase + @tagged convention per
CL-Odoo-test-class-conventions-odoo19.
"""
from odoo.tests import tagged

from .test_service_charge import TestServiceChargeCommon


@tagged('post_install', '-at_install', 'lw_cc_surcharge')
class TestServiceChargeRun(TestServiceChargeCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Explicit: the shipping default, and the mode whose per-invoice
        # attribution these tests are mostly about.
        cls.company.lw_cc_sc_mode = 'separate_invoice'

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run_and_get_record(self):
        """Run the runner and return (stats, run record)."""
        stats = self._run_runner()
        self.assertTrue(
            stats.get('run_id'),
            "the runner did not write a run record; _create_run_record "
            "swallowed an exception (check the server log).",
        )
        run = self.env['lw_cc.service.charge.run'].browse(stats['run_id'])
        self.assertTrue(run.exists())
        return stats, run

    def _lines_for(self, run, partner):
        return run.line_ids.filtered(lambda l: l.partner_id == partner)

    # ------------------------------------------------------------------
    # 1. Dry run: report yes, accounting objects no
    # ------------------------------------------------------------------

    def test_01_dry_run_writes_run_and_zero_account_moves(self):
        """A dry run writes the run + its lines and creates no
        account.move rows at all.

        Counting account.move before and after is the assertion that
        matters: run records ARE created in dry-run (they are audit
        objects and that is intended), so "nothing was created" is the
        wrong check -- "no ACCOUNTING object was created" is the right
        one.
        """
        self.company.lw_cc_surcharge_dry_run = True
        partner = self._create_partner(name='Run Record Dry Run Customer')
        self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )

        Move = self.env['account.move']
        moves_before = Move.search_count([])
        lines_before = self.env['account.move.line'].search_count([])

        stats, run = self._run_and_get_record()

        self.assertEqual(
            Move.search_count([]), moves_before,
            "dry-run created account.move rows; dry-run must create "
            "zero accounting objects.",
        )
        self.assertEqual(
            self.env['account.move.line'].search_count([]), lines_before,
            "dry-run created account.move.line rows.",
        )
        self.assertEqual(run.mode, 'dry_run')
        self.assertEqual(run.sc_mode, 'separate_invoice')
        self.assertIn('DRY-RUN', run.name)
        self.assertEqual(run.state, 'done')

        lines = self._lines_for(run, partner)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.action, 'would_charge')
        self.assertEqual(lines.reason, 'ok')
        self.assertAlmostEqual(lines.amount_residual, 1000.0, places=2)
        # 1.5% of 1000 = 15.00
        self.assertAlmostEqual(lines.computed_interest, 15.0, places=2)

        self.company.lw_cc_surcharge_dry_run = False

    # ------------------------------------------------------------------
    # 2. Suppression reasons are recorded
    # ------------------------------------------------------------------

    def test_02_optout_suppression_is_recorded(self):
        """An exempted partner gets a partner-level line carrying the
        reason, not silence."""
        partner = self._create_partner(
            name='Run Record Opted Out Customer',
            lw_cc_service_charge_optout=True,
        )
        self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )

        stats, run = self._run_and_get_record()

        lines = self._lines_for(run, partner)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.reason, 'optout')
        self.assertEqual(lines.action, 'skipped')
        # Partner-level row: no single invoice caused this.
        self.assertFalse(lines.move_id)
        # The residual is still reported, so the reader can see how much
        # money the exemption covers...
        self.assertAlmostEqual(lines.amount_residual, 1000.0, places=2)
        # ...but nothing was charged, so the money column stays 0.00.
        self.assertAlmostEqual(lines.computed_interest, 0.0, places=2)
        self.assertEqual(len(self._get_partner_charges(partner)), 0)

    def test_03_below_min_balance_suppression_is_recorded(self):
        """A partner under the minimum past-due balance gets a line
        with reason='below_min_balance'."""
        self.company.lw_cc_surcharge_min_balance = 500.0
        partner = self._create_partner(
            name='Run Record Small Balance Customer',
        )
        self._create_invoice(
            partner, 100.0, self._past_due_date(45), self.term_net30,
        )

        stats, run = self._run_and_get_record()

        lines = self._lines_for(run, partner)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.reason, 'below_min_balance')
        self.assertEqual(lines.action, 'skipped')
        self.assertFalse(lines.move_id)
        self.assertAlmostEqual(lines.amount_residual, 100.0, places=2)
        self.assertAlmostEqual(lines.computed_interest, 0.0, places=2)

        self.company.lw_cc_surcharge_min_balance = 10.0

    def test_04_already_charged_this_month_is_recorded(self):
        """A second run in the same month records the idempotency skip
        rather than dropping the partner from the report."""
        partner = self._create_partner(
            name='Run Record Idempotency Customer',
        )
        self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )

        _stats_first, first_run = self._run_and_get_record()
        self.assertEqual(
            self._lines_for(first_run, partner).action, 'charged',
        )

        _stats_second, second_run = self._run_and_get_record()
        self.assertNotEqual(first_run, second_run)

        lines = self._lines_for(second_run, partner)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.reason, 'already_charged_month')
        self.assertEqual(lines.action, 'skipped')
        # Still exactly one charge invoice, from the first run.
        self.assertEqual(len(self._get_partner_charges(partner)), 1)

    # ------------------------------------------------------------------
    # 3. Live mode: report AND charge invoice
    # ------------------------------------------------------------------

    def test_05_live_mode_writes_run_and_charge_invoice(self):
        """Live mode creates the run records AND the charge invoice, and
        the two agree on the amount."""
        self.assertFalse(self.company.lw_cc_surcharge_dry_run)
        partner = self._create_partner(name='Run Record Live Customer')
        self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )

        stats, run = self._run_and_get_record()

        self.assertEqual(run.mode, 'live')
        self.assertIn('LIVE', run.name)

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        self.assertAlmostEqual(charges.amount_total, 15.0, places=2)

        lines = self._lines_for(run, partner)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.action, 'charged')
        self.assertEqual(lines.reason, 'ok')
        self.assertAlmostEqual(
            sum(lines.mapped('computed_interest')),
            charges.amount_total, places=2,
            msg="the report disagrees with the charge invoice it describes.",
        )

    # ------------------------------------------------------------------
    # 4. Per-invoice breakdown
    # ------------------------------------------------------------------

    def test_06_per_invoice_breakdown_in_separate_invoice_mode(self):
        """A partner with two past-due invoices yields TWO lines, each
        carrying that invoice's residual and its share of the interest.

        This is the whole point of the report. One partner-level row for
        a charged partner would not answer "which invoices have the most
        interest accrued", which is the question that was asked.
        """
        partner = self._create_partner(name='Run Record Breakdown Customer')
        inv_small = self._create_invoice(
            partner, 500.0, self._past_due_date(45), self.term_net30,
        )
        inv_big = self._create_invoice(
            partner, 1500.0, self._past_due_date(60), self.term_net30,
        )

        stats, run = self._run_and_get_record()

        lines = self._lines_for(run, partner)
        self.assertEqual(len(lines), 2)
        self.assertEqual(
            set(lines.mapped('move_id').ids), {inv_small.id, inv_big.id},
        )

        by_move = {line.move_id.id: line for line in lines}
        self.assertAlmostEqual(
            by_move[inv_small.id].amount_residual, 500.0, places=2,
        )
        self.assertAlmostEqual(
            by_move[inv_big.id].amount_residual, 1500.0, places=2,
        )
        # 1.5% of 500 and of 1500.
        self.assertAlmostEqual(
            by_move[inv_small.id].computed_interest, 7.5, places=2,
        )
        self.assertAlmostEqual(
            by_move[inv_big.id].computed_interest, 22.5, places=2,
        )

        # The parts sum to the charge invoice exactly -- the drift
        # adjustment in _split_charge_across_moves exists so a reader
        # reconciling the report against the invoice never finds a
        # stray cent.
        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        self.assertAlmostEqual(
            sum(lines.mapped('computed_interest')),
            charges.amount_total, places=2,
        )

        # The default order IS the report: biggest interest first.
        ordered = self.env['lw_cc.service.charge.run.line'].search([
            ('run_id', '=', run.id),
            ('partner_id', '=', partner.id),
        ])
        self.assertEqual(ordered[0].move_id, inv_big)

    def test_07_per_invoice_breakdown_in_invoice_line_mode(self):
        """invoice_line mode reports one line per assessed invoice too,
        with the interest that actually landed on it."""
        self.company.lw_cc_sc_mode = 'invoice_line'
        try:
            partner = self._create_partner(
                name='Run Record Invoice Line Customer',
            )
            inv_1 = self._create_invoice(
                partner, 400.0, self._past_due_date(45), self.term_net30,
            )
            inv_2 = self._create_invoice(
                partner, 1000.0, self._past_due_date(60), self.term_net30,
            )

            stats, run = self._run_and_get_record()

            self.assertEqual(run.sc_mode, 'invoice_line')
            lines = self._lines_for(run, partner)
            self.assertEqual(len(lines), 2)
            by_move = {line.move_id.id: line for line in lines}
            self.assertEqual(by_move[inv_1.id].action, 'charged')
            self.assertEqual(by_move[inv_2.id].action, 'charged')
            # 1.5% of 400 = 6.00; 1.5% of 1000 = 15.00
            self.assertAlmostEqual(
                by_move[inv_1.id].computed_interest, 6.0, places=2,
            )
            self.assertAlmostEqual(
                by_move[inv_2.id].computed_interest, 15.0, places=2,
            )
            # The residual reported is the PRE-charge figure, not the
            # one the interest line just inflated.
            self.assertAlmostEqual(
                by_move[inv_2.id].amount_residual, 1000.0, places=2,
            )
        finally:
            self.company.lw_cc_sc_mode = 'separate_invoice'

    # ------------------------------------------------------------------
    # The seam: a rolled-back partner must still appear in the report
    # ------------------------------------------------------------------

    def test_08_partner_failure_survives_the_savepoint_rollback(self):
        """A partner whose processing raises still gets an audit row.

        The run line is contributed from OUTSIDE the per-partner
        savepoint for exactly this reason: written inside, the rollback
        that isolates the failure would also erase the only record
        explaining it, and the report would be silent about its single
        most interesting case.
        """
        partner = self._create_partner(name='Run Record Exploding Customer')
        self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )

        Runner = type(self.env['lw_cc.service.charge.runner'])

        def _boom(runner_self, *args, **kwargs):
            raise ValueError("simulated per-partner failure")

        self.patch(Runner, '_process_partner', _boom)

        stats, run = self._run_and_get_record()

        self.assertEqual(
            run.state, 'error',
            "a run containing an unexpected failure must not read as a "
            "clean run.",
        )
        lines = self._lines_for(run, partner)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines.reason, 'unexpected_error')
        self.assertEqual(lines.action, 'skipped')
        self.assertIn('simulated per-partner failure', lines.reason_detail)
        # The pre-run residual is preserved on the audit row even though
        # the savepoint was rolled back.
        self.assertAlmostEqual(lines.amount_residual, 1000.0, places=2)
        # Nothing was charged.
        self.assertEqual(len(self._get_partner_charges(partner)), 0)

    # ------------------------------------------------------------------
    # Header/detail consistency
    # ------------------------------------------------------------------

    def test_09_header_totals_match_the_detail_lines(self):
        """The header's Total Computed equals the sum of the detail
        lines' Computed Interest, and the partner counters add up.

        This is the invariant that lets the list view's column sum be
        read straight against the header: skipped lines carry 0.00, so
        suppressed money is never double-counted into the total.
        """
        charged = self._create_partner(name='Run Record Totals Charged')
        self._create_invoice(
            charged, 1000.0, self._past_due_date(45), self.term_net30,
        )
        skipped = self._create_partner(
            name='Run Record Totals Skipped',
            lw_cc_service_charge_optout=True,
        )
        self._create_invoice(
            skipped, 2000.0, self._past_due_date(45), self.term_net30,
        )

        stats, run = self._run_and_get_record()

        self.assertAlmostEqual(
            run.total_computed,
            sum(run.line_ids.mapped('computed_interest')),
            places=2,
        )
        self.assertEqual(
            run.partner_count,
            run.charged_partner_count + run.skipped_partner_count,
        )
        self.assertEqual(run.charged_partner_count, stats['partners_charged'])
        self.assertEqual(run.skipped_partner_count, stats['partners_skipped'])
        self.assertEqual(run.line_count, len(run.line_ids))
        # Both of this test's partners are represented.
        self.assertEqual(len(self._lines_for(run, charged)), 1)
        self.assertEqual(len(self._lines_for(run, skipped)), 1)
