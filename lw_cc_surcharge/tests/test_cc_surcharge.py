# -*- coding: utf-8 -*-
"""Tests for the CC surcharge at payment time.

Covers:
  - Surcharge invoice created after card payment for Net 30+ customer
  - No surcharge for Net 0 customer
  - No surcharge for ACH payment
  - BIN lookup: debit card exclusion
  - BIN lookup: unknown card = credit (conservative)
  - Percentage configurable
  - Company disabled = no surcharge
  - Surcharge invoice ref (CCS/<tx reference>) + joined origin
  - Dry-run suppresses the fee even with every other gate configured
    (19.0.5.0.0), paired with a live-mode test so "no fee" cannot pass
    for the wrong reason

The class runs in LIVE mode (dry_run False) -- see setUpClass.
"""
from datetime import timedelta

from odoo import Command, fields
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'lw_cc_surcharge')
class TestCCSurcharge(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.company = cls.env.company

        # Income account. Resolved BEFORE the company config write
        # below, which needs it for two reasons (see that write).
        Account = cls.env['account.account'].with_context(
            allowed_company_ids=cls.company.ids,
        )
        cls.income_account = Account.search([
            ('account_type', '=', 'income'),
        ], limit=1)
        if not cls.income_account:
            cls.income_account = Account.create({
                'name': 'Test CC Surcharge Income',
                'code': '999901',
                'account_type': 'income',
            })

        cls.company.write({
            'lw_cc_surcharge_enabled': True,
            # dry_run FALSE, deliberately, and do not set it back to
            # True "for safety" -- that would silently gut this class.
            #
            # This class tests LIVE credit card fee behaviour: most of
            # its tests assert that a fee invoice IS created. Until
            # 19.0.5.0.0 it could leave dry_run True and still see fees,
            # because the card-fee path did not read the flag at all --
            # dry_run was honoured only by the monthly interest runner.
            # That was the defect; 19.0.5.0.0 made dry_run gate BOTH
            # money paths (res_company._lw_cc_fee_is_dry_run), so
            # "live fee behaviour" now genuinely requires live mode.
            #
            # The dry-run side of the card fee is pinned by
            # test_15_dry_run_blocks_cc_fee_with_everything_configured
            # below, which sets it back to True for that one test.
            'lw_cc_surcharge_dry_run': False,
            'lw_cc_surcharge_cc_pct': 3.0,
            # Required by _check_live_mode_requires_income_account as
            # soon as dry_run goes False on an enabled company. This is
            # the Charge Terms Interest account, distinct from the CC
            # one on the next line; test_13 relies on it too.
            'lw_cc_surcharge_income_account_id': cls.income_account.id,
            'lw_cc_surcharge_cc_income_account_id': cls.income_account.id,
        })

        # Product.
        cls.product = cls.env.ref(
            'lw_cc_surcharge.product_service_charge',
            raise_if_not_found=False,
        )
        if cls.product:
            cls.company.lw_cc_surcharge_product_id = cls.product.id

        # Payment terms.
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

        cls.term_net30 = cls.env['account.payment.term'].create({
            'name': 'Test Net 30',
            'line_ids': [Command.create({
                'value': 'percent',
                'value_amount': 100.0,
                'nb_days': 30,
            })],
        })

        # Set applicable terms to Net 30+.
        cls.company.lw_cc_surcharge_applicable_term_ids = cls.term_net30

        cls.today = fields.Date.context_today(cls.env.user)

        # Test partner.
        cls.partner = cls.env['res.partner'].create({
            'name': 'CC Surcharge Test Customer',
            'company_id': cls.company.id,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_invoice(self, partner, amount, term=None):
        """Create and post a customer invoice."""
        move_vals = {
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_date': self.today - timedelta(days=1),
            'invoice_date_due': self.today + timedelta(days=1),
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

    def _create_transaction(self, invoice, payment_method_code='card', amount=None):
        """Create a done payment transaction for testing."""
        # Find or create payment method.
        pm = self.env['payment.method'].search([
            ('code', '=', payment_method_code),
        ], limit=1)
        if not pm:
            pm = self.env['payment.method'].create({
                'name': f'Test {payment_method_code}',
                'code': payment_method_code,
            })

        # Find or create provider.
        provider = self.env['payment.provider'].search([
            ('code', '=', 'authorize'),
        ], limit=1)
        if not provider:
            provider = self.env['payment.provider'].create({
                'name': 'Test Authorize.Net',
                'code': 'authorize',
                'state': 'enabled',
                'is_published': True,
            })

        tx_amount = amount or invoice.amount_total
        tx = self.env['payment.transaction'].create({
            'provider_id': provider.id,
            'provider_code': 'authorize',
            'payment_method_id': pm.id,
            'reference': f'TEST-CC-SURCHARGE-{self.id()}',
            'amount': tx_amount,
            'currency_id': self.company.currency_id.id,
            'partner_id': self.partner.id,
            'invoice_ids': [Command.set(invoice.ids)],
            'state': 'draft',
        })

        # Process the transaction (simulating a successful payment).
        # We call _process directly to simulate the payment callback.
        tx.write({'state': 'done'})
        return tx

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_01_surcharge_created_for_net30_card(self):
        """Net 30+ customer paying by card gets a surcharge invoice."""
        inv = self._create_invoice(self.partner, 1000.0, self.term_net30)
        tx = self._create_transaction(inv, 'card', 1000.0)

        # Run the surcharge assessment.
        tx._assess_cc_surcharge()

        # Check surcharge invoice was created.
        self.assertTrue(tx.lw_cc_surcharge_invoice_id)
        # 3% of 1000 = 30.00
        self.assertAlmostEqual(tx.lw_cc_surcharge_amount, 30.0, places=2)

    def test_02_no_surcharge_for_net0(self):
        """Net 0 customer paying by card gets no surcharge."""
        inv = self._create_invoice(self.partner, 1000.0, self.term_net0)
        tx = self._create_transaction(inv, 'card', 1000.0)

        tx._assess_cc_surcharge()

        self.assertFalse(tx.lw_cc_surcharge_invoice_id)

    def test_03_no_surcharge_for_ach(self):
        """ACH payment gets no surcharge."""
        inv = self._create_invoice(self.partner, 1000.0, self.term_net30)
        tx = self._create_transaction(inv, 'ach_direct_debit', 1000.0)

        tx._assess_cc_surcharge()

        self.assertFalse(tx.lw_cc_surcharge_invoice_id)

    def test_04_no_surcharge_when_disabled(self):
        """Company disabled = no surcharge."""
        self.company.lw_cc_surcharge_enabled = False
        inv = self._create_invoice(self.partner, 1000.0, self.term_net30)
        tx = self._create_transaction(inv, 'card', 1000.0)

        tx._assess_cc_surcharge()

        self.assertFalse(tx.lw_cc_surcharge_invoice_id)
        self.company.lw_cc_surcharge_enabled = True

    def test_05_surcharge_percentage_configurable(self):
        """Surcharge percentage is configurable."""
        self.company.lw_cc_surcharge_cc_pct = 2.5
        inv = self._create_invoice(self.partner, 1000.0, self.term_net30)
        tx = self._create_transaction(inv, 'card', 1000.0)

        tx._assess_cc_surcharge()

        self.assertTrue(tx.lw_cc_surcharge_invoice_id)
        # 2.5% of 1000 = 25.00
        self.assertAlmostEqual(tx.lw_cc_surcharge_amount, 25.0, places=2)
        self.company.lw_cc_surcharge_cc_pct = 3.0

    def test_06_bin_lookup_debit_skipped(self):
        """Debit card detected via BIN lookup skips surcharge."""
        # Create a BIN record for debit cards.
        self.env['lw_cc.bin.record'].create({
            'bin_start': '400000',
            'bin_end': '400005',
            'card_type': 'DEBIT',
            'network': 'VISA',
        })

        inv = self._create_invoice(self.partner, 1000.0, self.term_net30)
        tx = self._create_transaction(inv, 'card', 1000.0)

        # Set the BIN context to trigger debit detection.
        tx = tx.with_context(lw_cc_card_bin='400001')
        tx._assess_cc_surcharge()

        # Should be skipped because card is debit.
        self.assertFalse(tx.lw_cc_surcharge_invoice_id)
        self.assertEqual(tx.lw_cc_surcharge_bin_check, 'DEBIT')

    def test_07_bin_lookup_unknown_charged(self):
        """Unknown BIN = credit (conservative: apply surcharge)."""
        inv = self._create_invoice(self.partner, 1000.0, self.term_net30)
        tx = self._create_transaction(inv, 'card', 1000.0)

        # No BIN in context = unknown.
        tx._assess_cc_surcharge()

        # Should apply surcharge (unknown = credit).
        self.assertTrue(tx.lw_cc_surcharge_invoice_id)

    def test_08_bin_lookup_credit_charged(self):
        """Credit card detected via BIN lookup applies surcharge."""
        self.env['lw_cc.bin.record'].create({
            'bin_start': '411111',
            'bin_end': '411111',
            'card_type': 'CREDIT',
            'network': 'VISA',
        })

        inv = self._create_invoice(self.partner, 1000.0, self.term_net30)
        tx = self._create_transaction(inv, 'card', 1000.0)

        tx = tx.with_context(lw_cc_card_bin='411111')
        tx._assess_cc_surcharge()

        # Should apply surcharge (card is credit).
        self.assertTrue(tx.lw_cc_surcharge_invoice_id)
        self.assertEqual(tx.lw_cc_surcharge_bin_check, 'CREDIT')

    def test_09_brand_method_still_surcharged(self):
        """Gateway brand rewrite (visa) still surcharges: gate resolves to primary.

        payment_authorize._apply_updates rewrites payment_method_id to the
        brand record from the gateway's accountType, so by the time the
        surcharge gate runs a real Visa payment carries code='visa'. The
        gate must resolve through primary_payment_method_id to see 'card'.
        """
        inv = self._create_invoice(self.partner, 1000.0, self.term_net30)
        tx = self._create_transaction(inv, 'card', 1000.0)

        card_pm = tx.payment_method_id
        visa = self.env['payment.method'].search([('code', '=', 'visa')], limit=1)
        if not visa:
            visa = self.env['payment.method'].create({
                'name': 'Visa',
                'code': 'visa',
            })
        visa.primary_payment_method_id = card_pm.id
        tx.payment_method_id = visa

        tx._assess_cc_surcharge()

        self.assertTrue(tx.lw_cc_surcharge_invoice_id)
        self.assertAlmostEqual(tx.lw_cc_surcharge_amount, 30.0, places=2)

    def test_10_surcharge_invoice_ref_and_origin(self):
        """The surcharge invoice carries the CCS/<tx reference> ref and
        the joined paid-invoice names.

        account_move.py::_create_cc_surcharge_invoice stamps
        ``ref = "CCS/<transaction.reference>"`` (ties the charge to the
        payment that triggered it) and ``invoice_origin`` = the source
        invoice names joined with ", ".
        """
        inv1 = self._create_invoice(self.partner, 600.0, self.term_net30)
        inv2 = self._create_invoice(self.partner, 400.0, self.term_net30)
        tx = self._create_transaction(inv1, 'card', 1000.0)
        # Link the second paid invoice to the same transaction.
        tx.invoice_ids = [Command.set((inv1 + inv2).ids)]

        tx._assess_cc_surcharge()

        charge = tx.lw_cc_surcharge_invoice_id
        self.assertTrue(charge)
        self.assertEqual(charge.ref, "CCS/%s" % tx.reference)
        # Both linked invoices are eligible (Net 30); the origin is
        # their names joined with ", " (order follows the eligible
        # recordset, so compare as a set).
        self.assertTrue(charge.invoice_origin)
        self.assertEqual(
            set(charge.invoice_origin.split(", ")),
            {inv1.name, inv2.name},
        )

    # ------------------------------------------------------------------
    # lw_cc_surcharge_optout, split independently
    # from lw_cc_service_charge_optout (Charge Terms Interest)
    # ------------------------------------------------------------------

    def test_11_cc_optout_blocks_legacy_assess(self):
        """The new lw_cc_surcharge_optout flag blocks the legacy
        post-payment assessment (independently of the pre-existing
        lw_cc_service_charge_optout, which this test leaves off)."""
        self.partner.lw_cc_surcharge_optout = True
        try:
            inv = self._create_invoice(self.partner, 1000.0, self.term_net30)
            tx = self._create_transaction(inv, 'card', 1000.0)

            tx._assess_cc_surcharge()

            self.assertFalse(tx.lw_cc_surcharge_invoice_id)
        finally:
            self.partner.lw_cc_surcharge_optout = False

    def test_12_interest_optout_does_not_block_cc_fee(self):
        """Split independence, direction 1: Charge Terms Interest
        opt-out ON, CC-surcharge opt-out OFF -> the credit card fee
        still applies. This is the regression that protects the
        split-flag requirement (two separate flags, not one shared one)."""
        self.partner.lw_cc_service_charge_optout = True
        self.partner.lw_cc_surcharge_optout = False
        try:
            inv = self._create_invoice(self.partner, 1000.0, self.term_net30)
            tx = self._create_transaction(inv, 'card', 1000.0)

            tx._assess_cc_surcharge()

            self.assertTrue(tx.lw_cc_surcharge_invoice_id)
            # 3% of 1000 = 30.00
            self.assertAlmostEqual(tx.lw_cc_surcharge_amount, 30.0, places=2)
        finally:
            self.partner.lw_cc_service_charge_optout = False

    def test_13_cc_optout_does_not_block_interest_runner(self):
        """Split independence, direction 2: CC-surcharge opt-out ON,
        Charge Terms Interest opt-out OFF -> the monthly service charge
        runner still charges the customer.

        Calls ``_process_partner`` directly rather than
        ``_run_for_company``: test_service_charge.py documents that this
        suite runs against a staging clone carrying production data, so
        a company-wide run would also pick up every pre-existing
        past-due partner and requires an explicit exemption pass in that
        file's setUpClass to stay hermetic. ``_process_partner`` is the
        exact per-partner unit ``_run_for_company`` calls after grouping
        (service_charge_runner.py); calling it directly with only this
        test's own partner/invoice and explicit pct/min_balance args
        scopes the assertion to this test without touching that
        company-wide search at all.
        """
        partner = self.env['res.partner'].create({
            'name': 'CC Optout Interest Runner Customer',
            'company_id': self.company.id,
            'lw_cc_surcharge_optout': True,
        })
        # _create_service_charge_invoice needs its own (Charge Terms
        # Interest) income account. setUpClass sets it at class level
        # now (it became mandatory there once this class switched to
        # live mode), so this is belt-and-braces rather than the only
        # place it is set -- kept so the test stands on its own.
        self.company.lw_cc_surcharge_income_account_id = self.income_account.id

        inv = self._create_invoice(partner, 1000.0, self.term_net30)
        inv.invoice_date_due = self.today - timedelta(days=45)

        runner = self.env['lw_cc.service.charge.runner']
        result = runner._process_partner(
            partner, inv, self.company, self.today,
            pct=1.5, min_balance=10.0, dry_run=False,
        )

        self.assertTrue(result.get('charged'))
        # 1.5% of 1000 = 15.00
        self.assertAlmostEqual(result.get('charge_amount'), 15.0, places=2)

    # ------------------------------------------------------------------
    # Fail-closed configuration guard (F4)
    # ------------------------------------------------------------------

    def test_14_no_surcharge_when_terms_unset(self):
        """An EMPTY "Applicable Payment Terms" list fails CLOSED.

        With the surcharge enabled and a positive percentage but no
        applicable terms configured, the company is half-configured --
        nothing is eligible and no fee may be charged.

        Pre-fix, ``_assess_cc_surcharge`` read the empty set as
        "applies to everyone" (``if not applicable_terms or
        inv.invoice_payment_term_id in applicable_terms``), so this
        exact fixture produced a $30 surcharge invoice: the opposite
        of the Net 0 exclusion the setting exists to express, and a
        customer-visible overcharge on any company that enabled the
        percentage before picking its terms. Every other surface
        (portal quote/charge gates, backend Pay wizard) already bailed
        on an empty list; this closes the last one.
        """
        self.company.write({
            'lw_cc_surcharge_applicable_term_ids': [Command.clear()],
        })
        try:
            # Guard the fixture itself: the assertion below is only
            # meaningful with the terms genuinely cleared.
            self.assertFalse(
                self.company.lw_cc_surcharge_applicable_term_ids
            )
            self.assertTrue(self.company.lw_cc_surcharge_enabled)
            self.assertGreater(self.company.lw_cc_surcharge_cc_pct, 0.0)

            inv = self._create_invoice(self.partner, 1000.0, self.term_net30)
            tx = self._create_transaction(inv, 'card', 1000.0)

            tx._assess_cc_surcharge()

            self.assertFalse(tx.lw_cc_surcharge_invoice_id)
            self.assertFalse(tx.lw_cc_surcharge_amount)
            # Independent of the transaction's own fields: no CCS
            # charge invoice was persisted for this payment at all
            # (account_move.py stamps ref = "CCS/<tx reference>").
            self.assertFalse(self.env['account.move'].search([
                ('ref', '=', "CCS/%s" % tx.reference),
            ]))
        finally:
            self.company.write({
                'lw_cc_surcharge_applicable_term_ids': [
                    Command.set(self.term_net30.ids),
                ],
            })

    # ------------------------------------------------------------------
    # Dry-run gate on the card fee (19.0.5.0.0)
    # ------------------------------------------------------------------

    def test_15_dry_run_blocks_cc_fee_with_everything_configured(self):
        """Dry-run suppresses the card fee even when every OTHER gate
        passes: enabled, a positive percentage, AND applicable terms
        configured.

        This is the post-the configuration work configuration -- the one that does
        not exist on production yet and is the planned next step. It is
        the whole point of the test: the two guards that make the
        card-fee path safe today (cc_pct = 0.0 and an empty terms list)
        are exactly the two settings the configuration work consist of configuring,
        and on the day someone configures them, dry-run is the only
        thing left standing between a staging rehearsal and real fees on
        real customer payments.

        Until 19.0.5.0.0 nothing was standing there at all:
        ``lw_cc_surcharge_dry_run`` was read in exactly ONE place in
        the whole codebase (the monthly interest runner) and the card
        fee ignored it, so this fixture produced a $30 CCS invoice while
        Settings said "Dry-Run Mode". Goes red on the pre-fix engine.
        """
        self.company.lw_cc_surcharge_dry_run = True
        try:
            # Guard the fixture: this test is only meaningful if every
            # other gate genuinely passes, so a future change that
            # quietly turns one of them off cannot make it vacuous.
            self.assertTrue(self.company.lw_cc_surcharge_enabled)
            self.assertGreater(self.company.lw_cc_surcharge_cc_pct, 0.0)
            self.assertTrue(
                self.company.lw_cc_surcharge_applicable_term_ids
            )
            self.assertTrue(self.company._lw_cc_fee_is_dry_run())

            inv = self._create_invoice(self.partner, 1000.0, self.term_net30)
            tx = self._create_transaction(inv, 'card', 1000.0)
            lines_before = len(inv.line_ids)

            tx._assess_cc_surcharge()

            # No fee recorded on the transaction.
            self.assertFalse(tx.lw_cc_surcharge_invoice_id)
            self.assertFalse(tx.lw_cc_surcharge_amount)
            # No separate CCS invoice persisted (account_move.py stamps
            # ref = "CCS/<tx reference>"), asserted independently of the
            # transaction's own fields.
            self.assertFalse(self.env['account.move'].search([
                ('ref', '=', "CCS/%s" % tx.reference),
            ]))
            # No fee LINE on the source invoice either. The legacy
            # engine never adds one, but lw_cc_surcharge
            # does on its own paths, so assert the invoice is untouched
            # rather than only that the legacy artifact is absent.
            self.assertEqual(len(inv.line_ids), lines_before)
            if 'lw_cc_fee_line' in self.env['account.move.line']._fields:
                self.assertFalse(inv.line_ids.filtered(
                    lambda l: l.lw_cc_fee_line
                ))
        finally:
            self.company.lw_cc_surcharge_dry_run = False

    def test_16_live_mode_still_charges_the_cc_fee(self):
        """The companion to test_15: with dry-run OFF and the same
        fixture, the fee IS created.

        Without this pairing, test_15 would pass just as happily
        against a guard that broke the card fee outright -- "no fee was
        created" is the expected result of both a correct dry-run gate
        and a completely severed code path.
        """
        self.assertFalse(self.company.lw_cc_surcharge_dry_run)
        inv = self._create_invoice(self.partner, 1000.0, self.term_net30)
        tx = self._create_transaction(inv, 'card', 1000.0)

        tx._assess_cc_surcharge()

        self.assertTrue(tx.lw_cc_surcharge_invoice_id)
        # 3% of 1000 = 30.00
        self.assertAlmostEqual(tx.lw_cc_surcharge_amount, 30.0, places=2)
