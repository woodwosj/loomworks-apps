# -*- coding: utf-8 -*-
"""Tests for the monthly service charge on past-due AR (Track B).

Covers:
  - Charge invoice creation with correct amount
  - Dry-run mode: no invoices created, chatter logged
  - Partner dunning_hold does NOT suppress (opt-in decouple)
  - Email subscription opt-out does NOT suppress 
  - Per-customer opt-out gate: exempted -> skipped (default charged)
  - Minimum balance threshold
  - Payment-state filtering (paid invoices excluded)
  - Past-due date filtering (not-yet-due excluded)
  - Charge invoice ref (SC/<YYYY-MM>/<partner id>) + joined origin
  - Product-unset fallback to the module seed product
  - Ref-based idempotency survives a mid-run product swap

Tests run against the staging clone which contains production data.
All assertions are test-partner-focused: they check whether the test
partner's charge invoice was created (or not), rather than asserting
on global stats that include production invoices.

Test class follows the TransactionCase + @tagged convention per
CL-Odoo-test-class-conventions-odoo19.
"""
from datetime import timedelta

from odoo import Command, fields
from odoo.tests import TransactionCase, tagged


class TestServiceChargeCommon(TransactionCase):
    """Shared fixtures ONLY -- company config, accounts, terms, opt-out
    isolation, and the helper methods both concrete test classes below
    need. Deliberately holds NO ``test_*`` methods and is NOT
    ``@tagged`` -- it must never be discovered/run directly.

    Adverse review (BLOCKING): the two concrete mode classes
    (``TestServiceCharge`` for 'separate_invoice',
    ``TestServiceChargeInvoiceLine`` for 'invoice_line') used to have
    one inherit from the other. Python's unittest re-runs every
    INHERITED ``test_*`` method too, so every one of the parent's
    separate-invoice tests silently executed a SECOND time with the
    company in invoice_line mode, asserting separate-invoice artifacts
    that deliberately no longer exist in that mode -- reddening the
    entire base suite by construction, on any database. Both concrete
    classes now inherit from THIS class instead, and neither inherits
    the other, so a ``test_*`` method defined in one can never be
    collected or executed under the other's ``setUpClass``. Do not
    reintroduce a class-to-class inheritance between the two concrete
    classes below -- always add new fixtures/helpers here instead.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.company = cls.env.company
        cls.company.write({
            'lw_cc_surcharge_enabled': True,
            'lw_cc_surcharge_dry_run': True,
            'lw_cc_surcharge_pct': 1.5,
            'lw_cc_surcharge_min_balance': 10.0,
            'lw_cc_surcharge_past_due_days': 30,
        })

        # Income account for the charge line. In Odoo 19,
        # account.account may not expose company_id as a searchable
        # field; rely on record rules for company scoping.
        Account = cls.env['account.account'].with_context(
            allowed_company_ids=cls.company.ids,
        )
        cls.income_account = Account.search([
            ('account_type', '=', 'income'),
        ], limit=1)
        if not cls.income_account:
            cls.income_account = Account.create({
                'name': 'Test Service Charge Income',
                'code': '999900',
                'account_type': 'income',
            })
        cls.company.lw_cc_surcharge_income_account_id = cls.income_account.id
        cls.company.lw_cc_surcharge_dry_run = False

        # Receivable account for test invoices.
        cls.receivable_account = Account.search([
            ('account_type', '=', 'asset_receivable'),
        ], limit=1)

        # Service charge product.
        cls.product = cls.env.ref(
            'lw_cc_surcharge.product_service_charge',
            raise_if_not_found=False,
        )
        if cls.product:
            cls.company.lw_cc_surcharge_product_id = cls.product.id

        # Payment term: immediate (Net 0).
        cls.term_net0 = cls.env['account.payment.term'].search([
            ('name', 'ilike', 'immediate'),
        ], limit=1)
        if not cls.term_net0:
            cls.term_net0 = cls.env['account.payment.term'].create({
                'name': 'Test Immediate',
                'line_ids': [Command.create({
                    'value': 'percent',
                    'value_amount': 100.0,
                    'nb_days': 0,
                })],
            })

        # Payment term: Net 30.
        cls.term_net30 = cls.env['account.payment.term'].create({
            'name': 'Test Net 30',
            'line_ids': [Command.create({
                'value': 'percent',
                'value_amount': 100.0,
                'nb_days': 30,
            })],
        })

        cls.today = fields.Date.context_today(cls.env.user)

        # Opt-out isolation (19.0.3.0.0): on a data-bearing database
        # (staging/prod snapshot), every pre-existing customer with a
        # past-due residual is now eligible by default, so a runner
        # invocation inside a test would charge them all and poison the
        # company-wide stats assertions (test_16 saw partners_charged=23
        # on the fix/review33 staging snapshot). Exempt every partner
        # that already holds an open posted customer invoice; partners
        # created by the tests themselves default to non-exempt, so
        # they remain the only eligible ones. On a bare dev pod this is
        # a no-op. Rolled back with the test transaction.
        preexisting_debtors = cls.env['account.move'].sudo().search([
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('amount_residual', '>', 0),
            ('company_id', '=', cls.company.id),
        ]).mapped('commercial_partner_id')
        if preexisting_debtors:
            preexisting_debtors.write({'lw_cc_service_charge_optout': True})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_partner(self, **kwargs):
        """Create a customer partner for testing.

        Charged by default (opt-out semantics: nobody is exempt unless
        deliberately ticked); pass ``lw_cc_service_charge_optout=True``
        to exercise the exemption skip path (test_12).
        """
        vals = {
            'name': 'Test Customer',
        }
        vals.update(kwargs)
        return self.env['res.partner'].create(vals)

    def _create_invoice(self, partner, amount, due_date, term=None):
        """Create and post a customer invoice."""
        move_vals = {
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_date': due_date - timedelta(days=60),
            'invoice_date_due': due_date,
            'invoice_line_ids': [Command.create({
                'name': 'Test Product',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.income_account.id,
            })],
        }
        if term:
            move_vals['invoice_payment_term_id'] = term.id
        move = self.env['account.move'].create(move_vals)
        move.action_post()
        return move

    def _past_due_date(self, days_past=45):
        """Return a due date that is N days in the past."""
        return self.today - timedelta(days=days_past)

    def _get_partner_charges(self, partner):
        """Return charge invoices created for a specific partner.

        Filters by the service charge product on invoice lines so we
        only find charge invoices, not the test source invoices.
        """
        domain = [
            ('partner_id', '=', partner.id),
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
        ]
        if self.product:
            domain.append(
                ('invoice_line_ids.product_id', '=', self.product.id),
            )
        return self.env['account.move'].search(domain)

    def _run_runner(self):
        """Run the service charge runner for the test company."""
        runner = self.env['lw_cc.service.charge.runner']
        return runner._run_for_company(self.company, self.today)

    def _pay_invoice(self, invoice, amount=None):
        """Register a payment on an invoice via the wizard.

        Defaults to a full payment (``invoice.amount_total``); pass
        ``amount`` to register a partial payment instead. Shared by
        both concrete mode classes (partial-payment / reconciliation
        tests exist on both sides).
        """
        bank_journal = self.env['account.journal'].search([
            ('type', '=', 'bank'),
        ], limit=1)
        if not bank_journal:
            bank_journal = self.env['account.journal'].search([
                ('type', '=', 'cash'),
            ], limit=1)
        wizard = self.env['account.payment.register'].with_context(
            active_model='account.move',
            active_ids=invoice.ids,
        ).create({
            'amount': amount if amount is not None else invoice.amount_total,
            'journal_id': bank_journal.id if bank_journal else False,
        })
        wizard._create_payments()


@tagged('post_install', '-at_install', 'lw_cc_surcharge')
class TestServiceCharge(TestServiceChargeCommon):
    """Tests for the monthly service charge in 'separate_invoice' mode
    (today's default -- one lump-sum charge invoice per partner per
    month). Fixtures live in ``TestServiceChargeCommon``; this class
    holds only 'separate_invoice'-mode ``test_*`` methods -- see T-1
    in ``TestServiceChargeCommon``'s docstring for why this class must
    NOT be a base class for ``TestServiceChargeInvoiceLine``.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Explicit, not relied on as an implicit field default -- makes
        # this class's mode self-documenting and symmetric with
        # TestServiceChargeInvoiceLine's own explicit mode set below.
        cls.company.lw_cc_sc_mode = 'separate_invoice'

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_01_charge_invoice_created(self):
        """A past-due invoice generates a correctly-priced charge invoice."""
        partner = self._create_partner(name='Charge Test Customer')
        self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        self._run_runner()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        # 1.5% of 1000 = 15.00
        self.assertAlmostEqual(charges.amount_total, 15.0, places=2)

    def test_02_dry_run_no_invoice_created(self):
        """Dry-run mode logs charges but creates no invoices."""
        self.company.lw_cc_surcharge_dry_run = True
        partner = self._create_partner(name='Dry Run Customer')
        self._create_invoice(
            partner, 500.0, self._past_due_date(45), self.term_net30,
        )
        self._run_runner()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 0)

        self.company.lw_cc_surcharge_dry_run = False

    def test_03_dunning_hold_does_not_suppress(self):
        """Partner-level dunning_hold no longer gates the charge.

        Skip Dunning is a communications decision. An
        opted-in partner with dunning_hold=True IS charged. This test
        goes red on the pre-DEC runner (partner dunning_hold gate).
        """
        partner = self._create_partner(name='Dunning Hold Customer')
        if 'dunning_hold' not in partner._fields:
            self.skipTest("dunning_hold field not available")
        partner.dunning_hold = True
        self._create_invoice(
            partner, 2000.0, self._past_due_date(45), self.term_net30,
        )
        self._run_runner()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        # 1.5% of 2000 = 30.00
        self.assertAlmostEqual(charges.amount_total, 30.0, places=2)

    def test_04_min_balance_threshold(self):
        """Partners below the minimum balance are skipped."""
        self.company.lw_cc_surcharge_min_balance = 500.0
        partner = self._create_partner(name='Small Balance Customer')
        self._create_invoice(
            partner, 100.0, self._past_due_date(45), self.term_net30,
        )
        self._run_runner()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 0)

        self.company.lw_cc_surcharge_min_balance = 10.0

    def test_05_paid_invoice_excluded(self):
        """Paid invoices are not included in the charge calculation."""
        partner = self._create_partner(name='Paid Invoice Customer')
        inv = self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        self._pay_invoice(inv)
        self._run_runner()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 0)

    def test_06_not_yet_due_excluded(self):
        """Invoices within the grace period are excluded."""
        partner = self._create_partner(name='Not Yet Due Customer')
        future_due = self.today + timedelta(days=10)
        self._create_invoice(
            partner, 1000.0, future_due, self.term_net30,
        )
        self._run_runner()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 0)

    def test_07_multiple_invoices_aggregated(self):
        """Multiple past-due invoices are summed per partner."""
        partner = self._create_partner(name='Multi Invoice Customer')
        self._create_invoice(
            partner, 500.0, self._past_due_date(45), self.term_net30,
        )
        self._create_invoice(
            partner, 1500.0, self._past_due_date(60), self.term_net30,
        )
        self._run_runner()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        # 1.5% of (500 + 1500) = 1.5% of 2000 = 30.00
        self.assertAlmostEqual(charges.amount_total, 30.0, places=2)

    def test_08_module_disabled_no_op(self):
        """When the feature is disabled, the cron does nothing."""
        self.company.lw_cc_surcharge_enabled = False
        partner = self._create_partner(name='Disabled Customer')
        self._create_invoice(
            partner, 10000.0, self._past_due_date(90), self.term_net30,
        )

        runner = self.env['lw_cc.service.charge.runner']
        runner._run_monthly_service_charge()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 0)

        self.company.lw_cc_surcharge_enabled = True

    def test_09_email_subscription_does_not_suppress(self):
        """Dunning email opt-out no longer gates the charge.

        email subscriptions are a communications decision.
        An opted-in partner whose contact unchecked the dunning email
        subscription IS charged. Goes red on the pre-DEC runner.
        """
        partner = self._create_partner(name='Unsubscribed Customer')
        if hasattr(partner, 'email_sub_dunning'):
            partner.email_sub_dunning = False
        self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        self._run_runner()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        # 1.5% of 1000 = 15.00
        self.assertAlmostEqual(charges.amount_total, 15.0, places=2)

    def test_10_charge_percentage_configurable(self):
        """The charge percentage is configurable."""
        self.company.lw_cc_surcharge_pct = 2.0
        partner = self._create_partner(name='2 Percent Customer')
        self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        self._run_runner()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        # 2.0% of 1000 = 20.00
        self.assertAlmostEqual(charges.amount_total, 20.0, places=2)

        self.company.lw_cc_surcharge_pct = 1.5

    def test_11_idempotency_guard(self):
        """Running the runner twice in the same month does not duplicate charges."""
        partner = self._create_partner(name='Idempotency Customer')
        self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        self._run_runner()
        charges_after_first = self._get_partner_charges(partner)
        self.assertEqual(len(charges_after_first), 1)

        # Run again -- should be skipped by idempotency guard.
        self._run_runner()
        charges_after_second = self._get_partner_charges(partner)
        self.assertEqual(len(charges_after_second), 1)

    def test_12_opted_out_skipped(self):
        """A partner who is exempted is never charged, even though every
        other condition is eligible (past due, over minimum, no holds).

        This is the opt-out guarantee: every customer with a past-due
        balance is charged unless deliberately exempted.
        """
        partner = self._create_partner(
            name='Opted Out Customer',
            lw_cc_service_charge_optout=True,
        )
        self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        self._run_runner()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 0)

    def test_13_invoice_level_hold_partial_exclusion(self):
        """Invoice-level dunning_hold still excludes that invoice's residual.

        The runner keeps the invoice-level dunning_hold exclusion (disputed
        invoice protection) even though partner-level dunning_hold no
        longer gates the charge. An opted-in partner with one held and
        one non-held past-due invoice is charged only on the non-held
        residual; the partner is not skipped entirely.
        """
        if 'dunning_hold' not in self.env['account.move']._fields:
            self.skipTest("dunning_hold field not available")
        partner = self._create_partner(name='Partial Hold Customer')
        held = self._create_invoice(
            partner, 500.0, self._past_due_date(45), self.term_net30,
        )
        held.dunning_hold = True
        self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        self._run_runner()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        # 1.5% of 1000 (the non-held invoice only; the held 500 invoice
        # is excluded from the eligibility domain) = 15.00
        self.assertAlmostEqual(charges.amount_total, 15.0, places=2)

    def test_14_partial_payment_charged_on_residual_only(self):
        """A partially-paid invoice is charged on the residual, not the total.

        $1,000 invoice, partially paid $400 -> residual $600. The runner
        sums amount_residual (service_charge_runner.py ~line 280), so the
        charge must be 1.5% of 600 = 9.00, not 1.5% of the original 1000
        = 15.00. No prior test registers a partial payment, so a
        regression that summed amount_total instead of amount_residual
        would pass every other test in this file.
        """
        partner = self._create_partner(name='Partial Payment Customer')
        inv = self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        self._pay_invoice(inv, amount=400.0)
        self.assertEqual(inv.payment_state, 'partial')
        self._run_runner()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        # 1.5% of (1000 - 400) = 1.5% of 600 = 9.00
        self.assertAlmostEqual(charges.amount_total, 9.0, places=2)

    def test_15_min_balance_boundary_is_charged(self):
        """A residual exactly equal to min_balance IS charged, not skipped.

        float_compare(total_residual, min_balance, ...) < 0 (runner
        ~line 283-286) only skips STRICTLY below the threshold.
        min_balance defaults to 10.0 (setUpClass); a residual of exactly
        $10.00 must still produce a charge (1.5% of 10.00 = 0.15), not
        be skipped.
        """
        partner = self._create_partner(name='Exact Min Balance Customer')
        self._create_invoice(
            partner, 10.0, self._past_due_date(45), self.term_net30,
        )
        self._run_runner()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        self.assertAlmostEqual(charges.amount_total, 0.15, places=2)

    def test_16_charge_amount_rounds_to_currency_precision(self):
        """A charge whose raw product isn't a clean cent value gets rounded.

        $22.22 * 1.5% = 0.3333 (repeating); the correctly-rounded charge
        is $0.33 via float_round (runner ~line 295), not the raw 0.3333.
        Every other test in this file uses amounts whose 1.5% is already
        a clean cent value, so float_round never has anything to round;
        a broken rounding implementation would still pass all of them.
        Checked against the runner's own returned total at finer
        precision (places=3) so this test actually discriminates a
        missing/broken float_round call -- a bare 2-dp check on the
        posted invoice's amount_total would not, since Odoo's own
        Monetary field storage rounds to currency precision regardless.

        ``stats['charged_total']`` is a company-wide sum across every
        partner charged in this runner invocation, not scoped to this
        test's partner (t-hard finding). The ``partners_charged == 1``
        assertion makes that scope explicit and self-verifying: if any
        other partner is ever charged in the same run (e.g. once
        production opt-ins exist), this test fails loudly on the count
        rather than silently drawing a wrong conclusion from a merged
        total.
        """
        partner = self._create_partner(name='Rounding Pin Customer')
        self._create_invoice(
            partner, 22.22, self._past_due_date(45), self.term_net30,
        )
        stats = self._run_runner()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        self.assertAlmostEqual(charges.amount_total, 0.33, places=2)
        self.assertEqual(stats['partners_charged'], 1)
        self.assertAlmostEqual(stats['charged_total'], 0.33, places=3)

    def test_17_charge_invoice_has_no_tax(self):
        """The service charge invoice line carries no tax.

        A late fee / interest charge is not a taxable sale; the charge
        amount must equal the invoice total exactly. The invoice line
        dict in account_move.py::_create_service_charge_invoice
        explicitly sets ``tax_ids: [Command.clear()]`` (and the product
        data record ships with ``taxes_id`` cleared), so this holds
        regardless of the company's default output tax configuration.
        If that guard ever regresses, amount_total would silently
        exceed the intended 1.5% charge and this test must catch it.
        """
        partner = self._create_partner(name='No Tax Customer')
        self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        self._run_runner()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        self.assertFalse(charges.invoice_line_ids.tax_ids)
        self.assertAlmostEqual(
            charges.amount_total, charges.amount_untaxed, places=2,
        )
        # 1.5% of 1000 = 15.00, untaxed
        self.assertAlmostEqual(charges.amount_total, 15.0, places=2)

    def test_18_charge_invoice_ref_and_origin(self):
        """The charge invoice carries the SC/<YYYY-MM>/P<partner id> ref
        and the joined source invoice names.

        account_move.py::_create_service_charge_invoice stamps
        ``ref = "SC/<invoice-date YYYY-MM>/P<commercial partner id>"``
        (the idempotency domain matches this prefix) and
        ``invoice_origin`` = the source invoice names joined with ", ".
        """
        partner = self._create_partner(name='Ref And Origin Customer')
        inv1 = self._create_invoice(
            partner, 500.0, self._past_due_date(45), self.term_net30,
        )
        inv2 = self._create_invoice(
            partner, 1500.0, self._past_due_date(60), self.term_net30,
        )
        self._run_runner()

        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        # The ref month must be the invoice date's own month, and the
        # invoice date is the run date.
        self.assertEqual(
            charges.invoice_date, fields.Date.context_today(self.env.user),
        )
        self.assertEqual(
            charges.ref,
            "SC/%s/P%s" % (
                charges.invoice_date.strftime("%Y-%m"), partner.id,
            ),
        )
        # Two source invoices aggregated into one charge; the origin is
        # their names joined with ", " (order follows the eligible
        # recordset, so compare as a set).
        self.assertTrue(charges.invoice_origin)
        self.assertEqual(
            set(charges.invoice_origin.split(", ")),
            {inv1.name, inv2.name},
        )

    def test_19_product_unset_falls_back_to_seed_product(self):
        """Clearing the configured product falls back to the module
        seed product.

        account_move.py::_create_service_charge_invoice resolves the
        product as ``company.lw_cc_surcharge_product_id`` OR
        ``lw_cc_surcharge.product_service_charge``; with the company
        field cleared the seed product must carry the line and the
        invoice is still created.
        """
        self.company.lw_cc_surcharge_product_id = False
        partner = self._create_partner(name='Seed Fallback Customer')
        self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        self._run_runner()

        seed = self.env.ref(
            'lw_cc_surcharge.product_service_charge',
            raise_if_not_found=False,
        )
        self.assertTrue(seed)
        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        self.assertEqual(charges.invoice_line_ids.product_id, seed)
        # 1.5% of 1000 = 15.00, via the seed product line
        self.assertAlmostEqual(charges.amount_total, 15.0, places=2)

    def test_20_no_product_available_returns_empty_recordset(self):
        """With neither a configured nor the seed product resolvable,
        the creator returns an empty recordset without raising.

        The seed product is made unresolvable by removing its
        ir.model.data mapping (env.ref then returns None); the record
        itself is untouched. Rolled back with the test transaction.
        """
        self.company.lw_cc_surcharge_product_id = False
        self.env['ir.model.data'].search([
            ('module', '=', 'lw_cc_surcharge'),
            ('name', '=', 'product_service_charge'),
        ]).unlink()

        partner = self._create_partner(name='No Product Customer')
        source = self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )

        # Direct creator call: empty recordset, no exception.
        charge = self.env['account.move'].sudo()._create_service_charge_invoice(
            partner, 15.0, self.company, source,
        )
        self.assertFalse(charge)

        # Full runner pass: the partner is skipped, nothing created.
        stats = self._run_runner()
        self.assertEqual(stats['partners_charged'], 0)
        self.assertEqual(len(self._get_partner_charges(partner)), 0)

    def test_21_idempotency_survives_product_swap(self):
        """Re-runs create 0 charges, even after the configured product
        is swapped between runs.

        The idempotency domain
        (service_charge_runner.py::_partner_already_charged_this_month)
        matches the charge product OR the ``SC/<YYYY-MM>/`` ref prefix,
        so a mid-month product swap cannot re-arm duplicates: with a
        product-only domain this test's third run would charge again on
        the swapped product. Goes red on the pre-fix runner.
        """
        partner = self._create_partner(name='Product Swap Customer')
        self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )

        stats_first = self._run_runner()
        self.assertEqual(stats_first['partners_charged'], 1)
        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        first_ref = charges.ref
        month = first_ref.split('/')[1]

        # Unchanged second run: 0 new charges (plain idempotency).
        stats_second = self._run_runner()
        self.assertEqual(stats_second['partners_charged'], 0)

        # Swap the configured product and run a third time: the ref
        # clause must still catch it.
        alt_product = self.env['product.product'].create({
            'name': 'Test Alt Service Charge Product',
            'type': 'consu',
            'list_price': 0.0,
            'taxes_id': [Command.clear()],
        })
        self.company.lw_cc_surcharge_product_id = alt_product

        stats_third = self._run_runner()
        self.assertEqual(stats_third['partners_charged'], 0)

        # Mirror the runner's dedupe domain: only the original charge,
        # still carrying the original ref; nothing on the swapped
        # product was created.
        all_charges = self.env['account.move'].search([
            ('partner_id', '=', partner.id),
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('ref', '=like', 'SC/%s/P%%' % month),
        ])
        self.assertEqual(len(all_charges), 1)
        self.assertEqual(all_charges.ref, first_ref)
        self.assertFalse(self.env['account.move'].search([
            ('partner_id', '=', partner.id),
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('invoice_line_ids.product_id', '=', alt_product.id),
        ]))

@tagged('post_install', '-at_install', 'lw_cc_surcharge')
class TestServiceChargeInvoiceLine(TestServiceChargeCommon):
    """Tests for Charge Terms Interest as per-invoice lines
    (``lw_cc_sc_mode == 'invoice_line'``).

    Inherits ONLY ``TestServiceChargeCommon``'s ``setUpClass`` (company
    base config, income/receivable accounts, product, payment terms,
    and the pre-existing-debtor opt-out isolation for data-bearing
    databases) -- NOT ``TestServiceCharge``'s -- and switches the
    company into ``invoice_line`` mode on top of that. See T-1 in
    ``TestServiceChargeCommon``'s docstring: this class must never
    inherit from ``TestServiceCharge`` (or vice versa), or every
    ``test_*`` method defined on one silently re-runs under the
    other's mode too.

    Covers:
      - Line added once per invoice per month; idempotent on a second
        run in the same month
      - 'compound' vs 'simple' base -> different, asserted amounts
      - Partial-payment policy both ways: skip_partial skips; the
        rereconcile path adds the line AND the pre-existing partial
        payment's reconciliation survives (the single riskiest
        behaviour in the whole build)
      - Locked fiscal period -> skipped and counted, no write, no
        date shift
      - Hash-locked journal -> skipped and counted
      - dry_run -> zero lines, zero field writes, chatter only
      - Cross-mode transition guard, including the regression test for
        the false positive fixed in
        ``_partner_already_charged_this_month`` (an invoice dated this
        month carrying only an interest line must not block the
        partner's other invoices on a later run in the same month)
      - min_balance stays a partner-level gate, not per-invoice
      - Commission exclusion of an interest line
      - Aging: the interest line lands on the ORIGINAL invoice (ages
        with the principal), not a fresh current-dated one

    Test class follows the TransactionCase + @tagged convention per
    CL-Odoo-test-class-conventions-odoo19.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company.write({
            'lw_cc_sc_mode': 'invoice_line',
            'lw_cc_sc_compounding': 'compound',
            'lw_cc_sc_partial_policy': 'rereconcile',
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_invoice_with_prior_interest(
        self, partner, principal, prior_interest, due_date, term=None,
    ):
        """Create and post a past-due invoice that already carries one
        Charge Terms Interest line (simulating a PRIOR month's
        assessment), to exercise the simple-vs-compound base
        calculation without going through the runner first -- a real
        runner pass would stamp ``lw_cc_sc_last_assessed_month`` for
        the CURRENT month and mask the very base-calculation
        difference under test via the idempotency guard.
        """
        move_vals = {
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_date': due_date - timedelta(days=60),
            'invoice_date_due': due_date,
            'invoice_line_ids': [
                Command.create({
                    'name': 'Test Principal',
                    'quantity': 1,
                    'price_unit': principal,
                    'account_id': self.income_account.id,
                }),
                Command.create({
                    'name': 'Prior Month Interest',
                    'quantity': 1,
                    'price_unit': prior_interest,
                    'account_id': self.income_account.id,
                    'lw_cc_sc_interest_line': True,
                }),
            ],
        }
        if term:
            move_vals['invoice_payment_term_id'] = term.id
        move = self.env['account.move'].create(move_vals)
        move.action_post()
        return move

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_01_interest_line_added_once_per_month(self):
        """A line is added once per invoice per month; a second run in
        the same month is a no-op (idempotent on the month key).
        """
        partner = self._create_partner(name='Once Per Month Customer')
        inv = self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        stats_first = self._run_runner()
        self.assertEqual(stats_first['invoices_charged'], 1)

        interest_lines = inv.line_ids.filtered(
            lambda l: l.lw_cc_sc_interest_line
        )
        self.assertEqual(len(interest_lines), 1)
        self.assertEqual(
            inv.lw_cc_sc_last_assessed_month, self.today.strftime("%Y-%m"),
        )
        # compound base = full residual (1000, no prior interest);
        # 1.5% of 1000 = 15.00
        self.assertAlmostEqual(interest_lines.price_subtotal, 15.0, places=2)
        self.assertAlmostEqual(inv.lw_cc_sc_total_assessed, 15.0, places=2)

        # Second run, same month: no-op.
        stats_second = self._run_runner()
        self.assertEqual(stats_second['invoices_charged'], 0)
        interest_lines_after = inv.line_ids.filtered(
            lambda l: l.lw_cc_sc_interest_line
        )
        self.assertEqual(len(interest_lines_after), 1)
        self.assertAlmostEqual(inv.lw_cc_sc_total_assessed, 15.0, places=2)

    def test_02_compound_base_includes_prior_interest(self):
        """'compound' mode bases interest on the full residual, which
        already includes a prior month's interest line.
        """
        self.company.lw_cc_sc_compounding = 'compound'
        partner = self._create_partner(name='Compound Base Customer')
        inv = self._create_invoice_with_prior_interest(
            partner, 1000.0, 20.0, self._past_due_date(45), self.term_net30,
        )
        self.assertAlmostEqual(inv.amount_residual, 1020.0, places=2)
        existing_line_ids = set(inv.line_ids.ids)

        self._run_runner()

        new_line = inv.line_ids.filtered(
            lambda l: l.id not in existing_line_ids
        )
        self.assertEqual(len(new_line), 1)
        self.assertTrue(new_line.lw_cc_sc_interest_line)
        # compound: base = full residual (1000 principal + 20 prior
        # interest) = 1020; 1.5% of 1020 = 15.30
        self.assertAlmostEqual(new_line.price_subtotal, 15.30, places=2)

    def test_03_simple_base_excludes_prior_interest(self):
        """'simple' mode subtracts prior interest lines from the
        residual first, so interest is never charged on interest --
        DIFFERENT from the compound test's amount for the same
        principal and prior interest.
        """
        self.company.lw_cc_sc_compounding = 'simple'
        partner = self._create_partner(name='Simple Base Customer')
        inv = self._create_invoice_with_prior_interest(
            partner, 1000.0, 20.0, self._past_due_date(45), self.term_net30,
        )
        self.assertAlmostEqual(inv.amount_residual, 1020.0, places=2)
        existing_line_ids = set(inv.line_ids.ids)

        self._run_runner()

        new_line = inv.line_ids.filtered(
            lambda l: l.id not in existing_line_ids
        )
        self.assertEqual(len(new_line), 1)
        self.assertTrue(new_line.lw_cc_sc_interest_line)
        # simple: base = residual (1020) minus prior interest lines
        # (20) = 1000; 1.5% of 1000 = 15.00 -- differs from
        # test_02's 15.30 for the identical fixture.
        self.assertAlmostEqual(new_line.price_subtotal, 15.0, places=2)

        self.company.lw_cc_sc_compounding = 'compound'  # restore default

    def test_04_skip_partial_policy_skips_partial_invoice(self):
        """'skip_partial' never touches a partially-paid invoice."""
        self.company.lw_cc_sc_partial_policy = 'skip_partial'
        partner = self._create_partner(name='Skip Partial Customer')
        inv = self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        self._pay_invoice(inv, amount=400.0)
        self.assertEqual(inv.payment_state, 'partial')

        stats = self._run_runner()

        self.assertEqual(stats['skipped_partial'], 1)
        self.assertEqual(stats['invoices_charged'], 0)
        self.assertFalse(
            inv.line_ids.filtered(lambda l: l.lw_cc_sc_interest_line)
        )
        self.assertFalse(inv.lw_cc_sc_last_assessed_month)

        self.company.lw_cc_sc_partial_policy = 'rereconcile'  # restore

    def test_05_rereconcile_partial_policy_survives_reconciliation(self):
        """'rereconcile' adds the interest line AND the pre-existing
        partial payment's reconciliation survives -- the single
        riskiest behaviour in the whole build.
        """
        self.company.lw_cc_sc_partial_policy = 'rereconcile'
        partner = self._create_partner(name='Rereconcile Customer')
        inv = self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        self._pay_invoice(inv, amount=400.0)
        self.assertEqual(inv.payment_state, 'partial')
        self.assertAlmostEqual(inv.amount_residual, 600.0, places=2)

        receivable_line = inv.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable'
        )
        self.assertTrue(
            receivable_line.matched_debit_ids
            | receivable_line.matched_credit_ids,
            "sanity: the partial payment must already be reconciled "
            "before the run",
        )

        stats = self._run_runner()

        self.assertEqual(stats['invoices_charged'], 1)
        new_lines = inv.line_ids.filtered(
            lambda l: l.lw_cc_sc_interest_line
        )
        self.assertEqual(len(new_lines), 1)
        # compound base = residual = 600; 1.5% of 600 = 9.00
        self.assertAlmostEqual(new_lines.price_subtotal, 9.0, places=2)

        # Reconciliation survived: the receivable line still carries
        # its match to the $400 payment (not dropped by the line
        # write). The residual grew by EXACTLY the interest amount --
        # if the reconciliation had been lost instead of restored,
        # amount_residual would read something other than
        # (600 + 9) = 609 (e.g. the full new total 1009, if the
        # payment's match were dropped entirely).
        self.assertTrue(
            receivable_line.matched_debit_ids
            | receivable_line.matched_credit_ids,
            "the pre-existing partial payment's reconciliation did "
            "not survive the interest line write",
        )
        self.assertEqual(inv.payment_state, 'partial')
        self.assertAlmostEqual(inv.amount_residual, 609.0, places=2)

    def test_06_locked_period_skipped_and_counted(self):
        """A fiscal-year-locked invoice is skipped and counted, not
        written to, and its date is never shifted.

        Isolated to a FRESH, minimal company (adverse review, run on
        a staging pod against a production-clone database):
        writing fiscalyear_lock_date on the SHARED test company
        (self.company) triggers core's OWN _validate_locks check for
        unreconciled bank statement lines company-wide (verified in
        the real v19 source, account/models/company.py
        _validate_locks -> RedirectWarning('Show Unreconciled Bank
        Statement Line') when any exist dated on/before the new lock
        date). On a production clone, self.company has real
        historical bank statement lines, some unreconciled before any
        date recent enough to be useful here -- so the write itself
        raised before this test ever reached its own assertions. A
        brand-new company has zero bank statement lines by
        construction (that search is scoped company_id child_of the
        company being locked), so the exact same real core check
        cannot fail here regardless of what exists in production
        data. This still genuinely exercises
        account.move._check_fiscal_lock_dates() on a REAL locked
        company via the REAL runner -- not a no-op, not a
        workaround, not a weakened assertion.
        """
        fresh_company = self.env['res.company'].sudo().create({
            'name': 'Test Locked Period Co (Isolated)',
            'currency_id': self.company.currency_id.id,
        })
        Account = self.env['account.account'].sudo()
        income_account = Account.create({
            'name': 'Test Locked Period Income',
            'code': '999903',
            'account_type': 'income',
            'company_ids': [Command.set([fresh_company.id])],
        })
        receivable_account = Account.create({
            'name': 'Test Locked Period Receivable',
            'code': '999904',
            'account_type': 'asset_receivable',
            'reconcile': True,
            'company_ids': [Command.set([fresh_company.id])],
        })
        journal = self.env['account.journal'].sudo().create({
            'name': 'Test Locked Period Sales Journal',
            'code': 'TLPJ',
            'type': 'sale',
            'company_id': fresh_company.id,
            'default_account_id': income_account.id,
        })
        fresh_company.write({
            'lw_cc_surcharge_enabled': True,
            'lw_cc_surcharge_dry_run': False,
            'lw_cc_surcharge_pct': 1.5,
            'lw_cc_surcharge_min_balance': 10.0,
            'lw_cc_surcharge_past_due_days': 30,
            'lw_cc_surcharge_income_account_id': income_account.id,
            'lw_cc_surcharge_product_id': (
                self.product.id if self.product else False
            ),
            'lw_cc_sc_mode': 'invoice_line',
            'lw_cc_sc_compounding': 'compound',
            'lw_cc_sc_partial_policy': 'rereconcile',
        })
        partner = self.env['res.partner'].sudo().with_company(
            fresh_company,
        ).create({
            'name': 'Locked Period Isolated Customer',
            'company_id': fresh_company.id,
            'property_account_receivable_id': receivable_account.id,
        })
        due_date = self._past_due_date(45)
        inv = self.env['account.move'].sudo().with_company(
            fresh_company,
        ).create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'company_id': fresh_company.id,
            'journal_id': journal.id,
            'invoice_date': due_date - timedelta(days=60),
            'invoice_date_due': due_date,
            'invoice_line_ids': [Command.create({
                'name': 'Test Product',
                'quantity': 1,
                'price_unit': 1000.0,
                'account_id': income_account.id,
            })],
        })
        inv.action_post()
        original_invoice_date = inv.invoice_date
        original_line_count = len(inv.line_ids)

        # This write is the actual point of isolating to a fresh
        # company: on self.company (production-clone) it raised
        # RedirectWarning before reaching here; on a company with zero
        # bank statement lines, it must succeed cleanly.
        fresh_company.fiscalyear_lock_date = self.today + timedelta(days=1)

        stats = self.env[
            'lw_cc.service.charge.runner'
        ]._run_for_company(fresh_company, self.today)

        self.assertEqual(stats['skipped_locked'], 1)
        self.assertEqual(stats['invoices_charged'], 0)
        self.assertFalse(inv.lw_cc_sc_last_assessed_month)
        # Never touches move.date -- the invoice is not rewritten.
        self.assertEqual(inv.invoice_date, original_invoice_date)
        self.assertEqual(len(inv.line_ids), original_line_count)

    def test_07_hash_locked_journal_skipped_and_counted(self):
        """A hash-locked journal's invoice is skipped and counted.

        Uses a DEDICATED test journal (never shared with other tests
        or with historical/production data) so toggling
        ``restrict_mode_hash_table`` on and back off is safe: core
        refuses to unlock a journal that has ANY move with
        ``inalterable_hash`` set, which a shared/production journal on
        this staging clone could carry from real hashed history.
        """
        test_journal = self.env['account.journal'].create({
            'name': 'Test Hash Lock Journal',
            'code': 'THLJ',
            'type': 'sale',
            'company_id': self.company.id,
        })
        partner = self._create_partner(name='Hash Locked Customer')
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'journal_id': test_journal.id,
            'invoice_date': self._past_due_date(45) - timedelta(days=60),
            'invoice_date_due': self._past_due_date(45),
            'invoice_line_ids': [Command.create({
                'name': 'Test Product',
                'quantity': 1,
                'price_unit': 1000.0,
                'account_id': self.income_account.id,
            })],
        })
        inv.action_post()

        test_journal.restrict_mode_hash_table = True
        try:
            stats = self._run_runner()
        finally:
            # Safe: this invoice posted BEFORE the hash flag was set,
            # so it never carries inalterable_hash; the journal is
            # also brand new and used by nothing else in this test.
            test_journal.restrict_mode_hash_table = False

        self.assertEqual(stats['skipped_hashed'], 1)
        self.assertEqual(stats['invoices_charged'], 0)
        self.assertFalse(inv.lw_cc_sc_last_assessed_month)
        self.assertFalse(
            inv.line_ids.filtered(lambda l: l.lw_cc_sc_interest_line)
        )

    def test_08_dry_run_zero_writes(self):
        """dry_run performs zero field writes and zero line inserts --
        and, since the fix (adverse review), zero chatter writes
        either. It used to message_post a note per invoice; on
        production that is potentially thousands of customer-visible
        chatter notes from a single readiness check, despite this
        method's own "zero writes" claim. It now only logs.
        """
        self.company.lw_cc_surcharge_dry_run = True
        partner = self._create_partner(
            name='Dry Run Invoice Line Customer',
        )
        inv = self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        original_line_count = len(inv.line_ids)
        original_message_count = len(inv.message_ids)

        stats = self._run_runner()

        # Counted as "would charge" for visibility, but nothing wrote.
        self.assertEqual(stats['invoices_charged'], 1)
        self.assertFalse(inv.lw_cc_sc_last_assessed_month)
        self.assertEqual(inv.lw_cc_sc_total_assessed, 0.0)
        self.assertEqual(len(inv.line_ids), original_line_count)
        self.assertFalse(
            inv.line_ids.filtered(lambda l: l.lw_cc_sc_interest_line)
        )
        # Genuinely zero writes: not even a chatter note landed on the
        # customer-visible record.
        self.assertEqual(len(inv.message_ids), original_message_count)

        self.company.lw_cc_surcharge_dry_run = False  # restore

    def test_09_cross_mode_guard_blocks_after_separate_invoice(self):
        """A genuine separate SC invoice this month blocks
        invoice_line assessment entirely for that partner this month.
        """
        partner = self._create_partner(name='Cross Mode Guard Customer')
        inv = self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        # Fabricate a genuine separate-invoice SC charge directly (as
        # if the company had run in 'separate_invoice' mode earlier
        # this month), bypassing the runner so this test isolates the
        # guard itself.
        charge = self.env['account.move'].sudo()._create_service_charge_invoice(
            partner, 15.0, self.company, inv,
        )
        self.assertTrue(charge)

        stats = self._run_runner()

        self.assertEqual(stats['invoices_charged'], 0)
        self.assertFalse(
            inv.line_ids.filtered(lambda l: l.lw_cc_sc_interest_line)
        )
        self.assertFalse(inv.lw_cc_sc_last_assessed_month)

    def test_10_non_interest_same_product_line_does_not_trip_guard(self):
        """Regression (adverse review, worst finding of the
        build): a posted invoice carrying a line with the SAME SC
        product but WITHOUT the ``lw_cc_sc_interest_line`` marker --
        exactly the shape of lw_cc_surcharge's
        same-invoice CC fee line (``models/account_move.py``
        resolves the identical ``lw_cc_surcharge_product_id`` /
        seed-product fallback and never sets
        ``lw_cc_sc_interest_line``) -- must NOT be mistaken by
        ``_partner_already_charged_this_month`` for a genuine separate
        SC invoice, and must NOT block the partner's OTHER invoices
        from being assessed.

        This fabricates that data shape directly (a plain
        ``Command.create`` line carrying the SC product, no interest
        marker) WITHOUT depending on lw_cc_surcharge at
        all, to prove the fix without reaching into that module.

        Pre-fix, ``_partner_already_charged_this_month``'s now-removed
        "product match" check (Check 2) matched ANY posted invoice
        carrying a line with the SC product where that line was not
        marked as interest -- which is exactly what a CC fee line
        looks like. Effect: a partner who had ANY invoice carrying
        such a line was treated as "already charged this month" and
        got ZERO Charge Terms Interest that month across ALL their
        past-due invoices. This test's fixture invoice is deliberately
        NOT past due (see below), so it plays no eligibility role of
        its own -- the test isolates the guard, not the assessment.
        """
        partner = self._create_partner(
            name='Non-Interest Same-Product Regression Customer',
        )
        product = (
            self.company.lw_cc_surcharge_product_id
            or self.env.ref('lw_cc_surcharge.product_service_charge')
        )
        # Invoice A: NOT past due (invoice_date_due defaults to today
        # via the standard term-based compute) -- it plays no role in
        # the eligibility domain at all. Its only purpose is to carry
        # a line with the SC product and no interest marker, simulating
        # a CC fee line having landed on it (e.g. the customer paid it
        # by card this month).
        inv_a = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_date': self.today,
            'invoice_payment_term_id': self.term_net30.id,
            'invoice_line_ids': [
                Command.create({
                    'name': 'Test Product A',
                    'quantity': 1,
                    'price_unit': 1000.0,
                    'account_id': self.income_account.id,
                }),
                Command.create({
                    'name': (
                        'Simulated CC Fee Line (same product, no '
                        'interest marker)'
                    ),
                    'product_id': product.id,
                    'quantity': 1,
                    'price_unit': 30.0,
                    'account_id': self.income_account.id,
                    'tax_ids': [Command.clear()],
                }),
            ],
        })
        inv_a.action_post()
        self.assertNotEqual(inv_a.invoice_date_due, False)
        self.assertGreater(inv_a.invoice_date_due, self.today)

        # Invoice B: a genuinely past-due invoice, the one that should
        # get assessed this tick.
        inv_b = self._create_invoice(
            partner, 500.0, self._past_due_date(90), self.term_net30,
        )

        # Pre-fix, Check 2 would have found invoice A's non-interest
        # SC-product line and wrongly skipped the WHOLE partner --
        # invoices_charged would read 0, not 1.
        stats = self._run_runner()
        self.assertEqual(stats['invoices_charged'], 1)
        self.assertTrue(
            inv_b.line_ids.filtered(lambda l: l.lw_cc_sc_interest_line)
        )
        # Invoice A was never eligible (not past due) and is untouched.
        self.assertFalse(
            inv_a.line_ids.filtered(lambda l: l.lw_cc_sc_interest_line)
        )

    def test_11_min_balance_partner_level_not_per_invoice(self):
        """min_balance gates the PARTNER's aggregate residual, not
        each invoice individually -- two invoices each below the
        threshold are BOTH assessed once their sum clears it.
        """
        self.company.lw_cc_surcharge_min_balance = 500.0
        partner = self._create_partner(
            name='Aggregate Min Balance Customer',
        )
        inv_1 = self._create_invoice(
            partner, 300.0, self._past_due_date(45), self.term_net30,
        )
        inv_2 = self._create_invoice(
            partner, 300.0, self._past_due_date(60), self.term_net30,
        )
        # Neither invoice alone (300) clears the 500 threshold, but
        # their sum (600) does.
        stats = self._run_runner()

        self.assertEqual(stats['invoices_charged'], 2)
        self.assertTrue(
            inv_1.line_ids.filtered(lambda l: l.lw_cc_sc_interest_line)
        )
        self.assertTrue(
            inv_2.line_ids.filtered(lambda l: l.lw_cc_sc_interest_line)
        )

        self.company.lw_cc_surcharge_min_balance = 10.0  # restore

    def test_12_interest_line_excluded_from_commission(self):
        """An interest line is excluded from the commission report.

        The commission detail report is a SEPARATE module (no
        dependency edge either way) -- skip if its wizard model isn't
        registered on this build rather than failing on a missing
        module.
        """
        if 'commission.detail.report' not in self.env:
            self.skipTest(
                "commission detail report not installed",
            )
        partner = self._create_partner(
            name='Commission Exclusion Customer',
        )
        inv = self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        self._run_runner()

        interest_lines = inv.line_ids.filtered(
            lambda l: l.lw_cc_sc_interest_line
        )
        self.assertEqual(len(interest_lines), 1)

        wizard = self.env['commission.detail.report'].create({
            'date_from': self.today.replace(day=1),
            'date_to': self.today,
        })
        self.assertTrue(wizard._is_excluded_line(interest_lines))

    def test_12b_sc_product_line_excluded_from_commission(self):
        """A service-charge / CC-surcharge PRODUCT line is excluded
        from the commission report too.

        test_12 covers one half of
        ``commission_detail_report._is_charge_line``: the
        ``account.move.line.lw_cc_sc_interest_line`` marker written by
        'invoice_line' mode. This covers the OTHER half -- a line
        carrying the product configured on
        ``res.company.lw_cc_surcharge_product_id`` -- which is the
        shape of BOTH a 'separate_invoice' service-charge line and
        lw_cc_surcharge's same-invoice CC fee line.
        Neither of those ever sets the interest marker, so the product
        match is the only thing standing between a fee line on a real
        customer invoice and a rep being paid commission on it.

        The fixture is deliberately a NORMAL customer invoice (not a
        dedicated charge invoice) carrying one ordinary product line
        and one charge-product line: that is exactly the pre-existing
        gap ``_is_excluded_line``'s docstring describes, where a charge
        landed on a real invoice and went commissionable under the
        catch-all plan rule like any other product line.

        Same skip guard as test_12: the commission detail report is a
        SEPARATE module with no dependency edge in either direction, so
        skip rather than fail when it is not on this build.
        """
        if 'commission.detail.report' not in self.env:
            self.skipTest(
                "commission detail report not installed",
            )
        charge_product = (
            self.company.lw_cc_surcharge_product_id
            or self.env.ref('lw_cc_surcharge.product_service_charge')
        )
        self.assertTrue(charge_product)

        # A plain product: not the charge product, and not one of
        # _is_shipping_line's excluded names/categories, so the only
        # helper that could exclude it is _is_charge_line itself.
        plain_product = self.env['product.product'].create({
            'name': 'Test Commission Fabric Bolt',
            'type': 'consu',
            'list_price': 0.0,
            'taxes_id': [Command.clear()],
        })

        partner = self._create_partner(
            name='Commission Product Exclusion Customer',
        )
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_date': self.today,
            'invoice_payment_term_id': self.term_net30.id,
            'invoice_line_ids': [
                Command.create({
                    'name': 'Regular Fabric Line',
                    'product_id': plain_product.id,
                    'quantity': 1,
                    'price_unit': 1000.0,
                    'account_id': self.income_account.id,
                    'tax_ids': [Command.clear()],
                }),
                Command.create({
                    'name': 'Credit Card Processing Fee',
                    'product_id': charge_product.id,
                    'quantity': 1,
                    'price_unit': 30.0,
                    'account_id': self.income_account.id,
                    'tax_ids': [Command.clear()],
                }),
            ],
        })
        inv.action_post()

        charge_line = inv.invoice_line_ids.filtered(
            lambda l: l.product_id == charge_product
        )
        plain_line = inv.invoice_line_ids.filtered(
            lambda l: l.product_id == plain_product
        )
        self.assertEqual(len(charge_line), 1)
        self.assertEqual(len(plain_line), 1)
        # Neither line carries the interest marker -- the product
        # match is doing all the work in this test.
        self.assertFalse(charge_line.lw_cc_sc_interest_line)
        self.assertFalse(plain_line.lw_cc_sc_interest_line)

        wizard = self.env['commission.detail.report'].create({
            'date_from': self.today.replace(day=1),
            'date_to': self.today,
        })
        self.assertTrue(wizard._is_charge_line(charge_line))
        self.assertFalse(wizard._is_charge_line(plain_line))
        # ...and the aggregate gate the report actually calls agrees.
        self.assertTrue(wizard._is_excluded_line(charge_line))
        self.assertFalse(wizard._is_excluded_line(plain_line))

    def test_13_interest_line_ages_with_principal(self):
        """The interest line lands on the ORIGINAL invoice -- it ages
        WITH the principal (in its own, old, aging bucket), not on a
        fresh current-dated invoice. No new account.move is created
        for the partner.
        """
        partner = self._create_partner(name='Aging Customer')
        old_due_date = self._past_due_date(120)
        inv = self._create_invoice(
            partner, 1000.0, old_due_date, self.term_net30,
        )
        original_invoice_date = inv.invoice_date
        original_due_date = inv.invoice_date_due

        self._run_runner()

        new_lines = inv.line_ids.filtered(
            lambda l: l.lw_cc_sc_interest_line
        )
        self.assertEqual(len(new_lines), 1)
        # The line landed on the SAME invoice; dates never shifted.
        self.assertEqual(inv.invoice_date, original_invoice_date)
        self.assertEqual(inv.invoice_date_due, original_due_date)
        # No new invoice was created for this partner -- the original
        # invoice is still the only out_invoice on record for them.
        all_partner_invoices = self.env['account.move'].search([
            ('partner_id', '=', partner.id),
            ('move_type', '=', 'out_invoice'),
        ])
        self.assertEqual(len(all_partner_invoices), 1)
        self.assertEqual(all_partner_invoices, inv)

    def test_14_symmetric_guard_blocks_separate_invoice_after_lines(self):
        """The double-charge regression this guard exists to prevent:
        interest already assessed via 'invoice_line' mode this month
        must block a same-month 'separate_invoice' charge for the
        same partner -- the designed rollback path (flip back to the
        safe default mid-month) must not double-charge. Fails against
        the pre-fix code, which had no guard in this direction.

         fixture (adverse review -- my own spec error in the
        FIRST version of this guard, not a coding mistake): the
        assessed invoice is paid IN FULL before the rollback run, so
        it drops OUT of the eligibility domain (amount_residual > 0)
        -- exactly the scenario the reviewer traced: "an invoice
        assessed by lines on 09-03 and paid in full on 09-20 leaves
        the eligibility domain, so on a 09-25 rollback its marker is
        invisible" to a guard that only looks at the CURRENT run's
        eligible invoices. A SECOND invoice, created only AFTER the
        first run (so it was never itself assessed and carries no
        marker), keeps the partner appearing in this run's eligible
        set -- otherwise the partner would not be processed at all
        this tick, and the bug could not manifest. This fixture is
        deliberately built so the OLD (moves-filtered) guard implementation
        finds NOTHING in its narrowed view -- inv_a is paid off and
        gone from `moves`, inv_b was never marked -- and would
        incorrectly proceed to charge inv_b's residual. Only a search
        across ALL of the partner's posted invoices (the fix) finds
        inv_a's marker and skips the partner entirely.
        """
        partner = self._create_partner(name='Rollback Guard Customer')
        inv_a = self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        # invoice_line mode is this class's default (setUpClass); run
        # it first, as if the operator had it live for part of the
        # month. Only inv_a exists yet, so only inv_a is assessed.
        stats_first = self._run_runner()
        self.assertEqual(stats_first['invoices_charged'], 1)
        self.assertTrue(
            inv_a.line_ids.filtered(lambda l: l.lw_cc_sc_interest_line)
        )
        # compound base = 1000; 1.5% of 1000 = 15.00
        self.assertAlmostEqual(inv_a.amount_residual, 1015.0, places=2)
        self.assertEqual(
            inv_a.lw_cc_sc_last_assessed_month, self.today.strftime("%Y-%m"),
        )

        # Pay inv_a IN FULL -- it now has amount_residual == 0 and
        # drops out of the eligibility domain entirely. Its marker
        # (lw_cc_sc_last_assessed_month) is untouched by payment.
        self._pay_invoice(inv_a)
        # Not asserting a specific payment_state value ('paid' vs.
        # 'in_payment' depends on which journal type _pay_invoice
        # resolves) -- what actually matters, and what the runner's
        # own eligibility domain checks, is that it is no longer
        # 'not_paid'/'partial'. amount_residual == 0 is the real proof.
        self.assertNotIn(inv_a.payment_state, ('not_paid', 'partial'))
        self.assertAlmostEqual(inv_a.amount_residual, 0.0, places=2)

        # inv_b: created only NOW, after run 1 -- it was never
        # eligible during run 1 (did not exist), so it carries no
        # marker of its own. It is what keeps the partner appearing
        # in the rollback run's eligible set.
        inv_b = self._create_invoice(
            partner, 500.0, self._past_due_date(90), self.term_net30,
        )
        self.assertFalse(inv_b.lw_cc_sc_last_assessed_month)

        invoices_before_rollback = self.env['account.move'].search([
            ('partner_id', '=', partner.id),
            ('move_type', '=', 'out_invoice'),
        ])
        self.assertEqual(len(invoices_before_rollback), 2)  # sanity

        # Operator rollback: flip back to the safe default mid-month.
        self.company.lw_cc_sc_mode = 'separate_invoice'
        stats_second = self._run_runner()

        # partners_charged is a safe, hermetic assertion regardless of
        # database size: NO partner -- real or fixture -- should ever
        # be charged in this scenario (real partners are opt-out-
        # isolated by setUpClass; this fixture partner is guard-
        # blocked). NOT asserting stats_second['partners_skipped']
        # here (adverse review, run on a staging pod against a
        # production-clone database): that counter is company-wide --
        # it also counts every real opted-out LwCc customer with an
        # eligible invoice this tick (23 on that pod), not just this
        # fixture partner, so pinning it to 1 is not hermetic against
        # a data-bearing database. The fact this specific partner WAS
        # in the skipped set is proven below in a partner-scoped way
        # instead, which is a strictly stronger proof: inv_b (created
        # above) is genuinely eligible, so the partner necessarily
        # appears in this run's per-partner processing at all; combined
        # with partners_charged == 0 here, they can only have landed
        # in "skipped" -- there is no third outcome.
        self.assertEqual(stats_second['partners_charged'], 0)

        # No new (separate SC) invoice was created for this partner --
        # counting account.move records directly, not via the
        # product-based _get_partner_charges helper, since inv_a
        # ITSELF now legitimately carries the SC product on its
        # interest line and would otherwise be miscounted as a
        # "charge".
        invoices_after_rollback = self.env['account.move'].search([
            ('partner_id', '=', partner.id),
            ('move_type', '=', 'out_invoice'),
        ])
        self.assertEqual(invoices_after_rollback, invoices_before_rollback)
        # inv_b was NOT charged -- residual unchanged, no interest
        # line (invoice_line mode never touched it either).
        self.assertAlmostEqual(inv_b.amount_residual, 500.0, places=2)
        self.assertFalse(
            inv_b.line_ids.filtered(lambda l: l.lw_cc_sc_interest_line)
        )

        self.company.lw_cc_sc_mode = 'invoice_line'  # restore default

    def test_15_symmetric_guard_does_not_over_fire_on_untouched_partner(self):
        """A partner never assessed by invoice_line lines this month
        is charged normally in 'separate_invoice' mode -- the new
        guard is a no-op on the normal (today's shipping) path.
        """
        self.company.lw_cc_sc_mode = 'separate_invoice'
        partner = self._create_partner(name='Untouched Guard Customer')
        self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        stats = self._run_runner()

        self.assertEqual(stats['partners_charged'], 1)
        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        # 1.5% of 1000 = 15.00
        self.assertAlmostEqual(charges.amount_total, 15.0, places=2)

        self.company.lw_cc_sc_mode = 'invoice_line'  # restore default

    def test_16_stale_marker_from_previous_month_does_not_block(self):
        """A lw_cc_sc_last_assessed_month stamped in a PRIOR month
        must NOT block this month's separate_invoice charge -- the
        guard compares against the CURRENT month key, not "ever
        assessed by lines at any point".
        """
        self.company.lw_cc_sc_mode = 'separate_invoice'
        partner = self._create_partner(name='Stale Marker Customer')
        inv = self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        # Simulate a PRIOR month's invoice_line assessment directly
        # (bypassing the runner, which would only ever stamp the
        # CURRENT month) -- a plain write is legal here: this custom
        # field is not in account.move's unmodifiable_fields list, so
        # no skip_readonly_check context is needed even on a posted
        # move.
        last_month = (
            self.today.replace(day=1) - timedelta(days=1)
        ).strftime("%Y-%m")
        inv.lw_cc_sc_last_assessed_month = last_month

        stats = self._run_runner()

        self.assertEqual(stats['partners_charged'], 1)
        charges = self._get_partner_charges(partner)
        self.assertEqual(len(charges), 1)
        self.assertAlmostEqual(charges.amount_total, 15.0, places=2)

        self.company.lw_cc_sc_mode = 'invoice_line'  # restore default

    def test_17_skipped_other_bucket_for_unexpected_failure(self):
        """An UNEXPECTED failure (no product configured, and no seed
        product resolvable either) lands in skipped_other with the
        real exception message logged -- NOT silently folded into
        skipped_locked (adverse review).

        Pre-fix, the write attempt's single generic ``except
        UserError`` re-inspected only hash-lock and partial-payment
        state, dumping every OTHER failure (including this one) into
        skipped_locked. A staging dry run reporting skipped_locked=40
        would read as "40 invoices in a closed period, expected" when
        some or all of them were actually failing on a missing
        company config field -- the pre-flip readiness gate would
        pass on a false reading.
        """
        partner = self._create_partner(name='Skipped Other Customer')
        self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        # Make the product unresolvable: clear the company's
        # configured product AND remove the seed product's
        # ir.model.data mapping (env.ref then returns None), mirroring
        # the base class's test_20 technique. Rolled back with the
        # test transaction.
        self.company.lw_cc_surcharge_product_id = False
        self.env['ir.model.data'].search([
            ('module', '=', 'lw_cc_surcharge'),
            ('name', '=', 'product_service_charge'),
        ]).unlink()

        stats = self._run_runner()

        self.assertEqual(stats['skipped_other'], 1)
        self.assertEqual(stats['skipped_locked'], 0)
        self.assertEqual(stats['skipped_hashed'], 0)
        self.assertEqual(stats['skipped_partial'], 0)
        self.assertEqual(stats['invoices_charged'], 0)

    def test_18_is_manually_modified_stays_false(self):
        """An automated cron write must never flag a customer invoice
        as hand-edited (adverse review).

        Core account.move.write() force-sets is_manually_modified=True
        on any write whose vals don't already include that key, unless
        skip_is_manually_modified=True is in context (verified in the
        real v19 source). Both writes this method makes -- the
        interest line (via _lw_cc_add_charge_line, shared with the CC
        fee path) and the lw_cc_sc_last_assessed_month/
        lw_cc_sc_total_assessed marker stamp -- now pass that context
        key. is_manually_modified is core's own signal for vendor-bill
        auto-post heuristics (_show_autopost_bills_wizard); an
        automated cron permanently flagging every invoice it ever
        touches as "manually modified" is wrong regardless of which
        UI surface currently reads the field.
        """
        partner = self._create_partner(name='Not Manually Modified Customer')
        inv = self._create_invoice(
            partner, 1000.0, self._past_due_date(45), self.term_net30,
        )
        # Sanity: creating + posting a normal invoice does not itself
        # set this (core already skips it during its own post flow).
        self.assertFalse(inv.is_manually_modified)

        self._run_runner()

        self.assertTrue(
            inv.line_ids.filtered(lambda l: l.lw_cc_sc_interest_line)
        )
        self.assertEqual(
            inv.lw_cc_sc_last_assessed_month, self.today.strftime("%Y-%m"),
        )
        self.assertFalse(inv.is_manually_modified)

    def test_19_simple_mode_excludes_cc_fee_line(self):
        """'simple' mode excludes CC processing fee lines from the
        base, not just prior interest lines (adverse review):
        'simple' mode exists precisely so interest does NOT compound,
        so a credit card processing fee sitting on the invoice must
        not be treated as principal and charged interest either.

        lw_cc_fee_line is declared by this module's CC-fee extension
        class -- skip if the field isn't registered on this build
        (a build variant without that class) rather than failing on
        a missing field. The
        runner's getattr guard makes that combination a safe no-op in
        production too (nothing to exclude if the marker can't exist);
        this test just can't exercise the "fee actually excluded" half
        without the field existing to mark a line with, so it fabricates
        the marked line directly as a data fixture (Command.create with
        lw_cc_fee_line=True) rather than reaching into the
        extension class to generate a real one -- the same
        technique as the earlier fix
        regression fixture.
        """
        if 'lw_cc_fee_line' not in self.env['account.move.line']._fields:
            self.skipTest(
                "lw_cc_fee_line not registered -- "
                "lw_cc_surcharge not installed",
            )
        self.company.lw_cc_sc_compounding = 'simple'
        partner = self._create_partner(name='CC Fee Exclusion Customer')
        due_date = self._past_due_date(45)
        # No payment term, deliberately -- see the no-term-echo comment
        # on test_10's fixture in this same class: an explicit
        # invoice_date_due only survives action_post unchanged when no
        # term is set.
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_date': due_date - timedelta(days=60),
            'invoice_date_due': due_date,
            'invoice_line_ids': [
                Command.create({
                    'name': 'Test Principal',
                    'quantity': 1,
                    'price_unit': 1000.0,
                    'account_id': self.income_account.id,
                }),
                Command.create({
                    'name': 'Simulated CC Processing Fee Line',
                    'quantity': 1,
                    'price_unit': 30.0,
                    'account_id': self.income_account.id,
                    'tax_ids': [Command.clear()],
                    'lw_cc_fee_line': True,
                }),
            ],
        })
        inv.action_post()
        self.assertAlmostEqual(inv.amount_residual, 1030.0, places=2)

        self._run_runner()

        new_lines = inv.line_ids.filtered(
            lambda l: l.lw_cc_sc_interest_line
        )
        self.assertEqual(len(new_lines), 1)
        # simple: base = residual (1030) minus the CC fee line (30)
        # = 1000; 1.5% of 1000 = 15.00 -- NOT 1.5% of 1030 = 15.45,
        # which is what pre-fix code would have charged by treating
        # the fee as principal.
        self.assertAlmostEqual(new_lines.price_subtotal, 15.0, places=2)

        self.company.lw_cc_sc_compounding = 'compound'  # restore default
