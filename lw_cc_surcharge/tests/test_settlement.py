# -*- coding: utf-8 -*-
"""Settlement contract of the portal CC surcharge uplift and the
same-invoice CC fee line.

One payment settles the invoice, GROWN by a same-invoice fee line,
with zero orphan credit (default: the fee is a line on the SAME
invoice, not a separate CCS invoice -- see the design notes).
No CCS invoice exists before the payment is created (nothing is booked
on a quote or an abandoned transaction).
The legacy post-payment engine of lw_cc_surcharge keeps running for
everything the uplift does not own (flag off / no invoice link), and is
skipped when the uplift already charged the fee or owns the invoice tx.

Additional same-invoice coverage below:
  - idempotency of ``_lw_cc_apply_cc_fee`` across repeated calls
  - the double-charge regression a savepoint closes (a failure AFTER the
    fee line write, during re-reconciliation, must roll the line back
    before the CCS fallback invoice is created -- never both)
  - a pre-existing partial payment survives the fee-line write
    (allow_partial=True's riskiest mechanism)
  - a hash-locked journal falls back to the separate CCS invoice
  - a multi-invoice overdue batch puts the fee on the OLDEST invoice by
    due date, with an id tie-break

Fixture mirrors lw_cc_surcharge/tests/test_cc_surcharge.py (company flags,
income account, product, Net 30 term, partner) and adds the provider +
journal + inbound payment method line that account_payment's
``_create_payment`` needs.
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import float_round, mute_logger


@tagged('post_install', '-at_install', 'lw_cc_surcharge')
class TestPortalUpliftSettlement(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.company = cls.env.company
        # Armed uplift by default: the uplift path is exercised; the legacy test flips
        # the flag inside its own test; per-test writes roll back with the
        # test savepoint, so no manual restore is needed.
        cls.company.write({
            'lw_cc_surcharge_enabled': True,
            'lw_cc_surcharge_cc_pct': 3.0,
            'lw_cc_surcharge_portal_uplift': True,
        })

        # Income account (same fallback chain as the base suite).
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
        cls.company.lw_cc_surcharge_cc_income_account_id = cls.income_account.id

        # 19.0.5.0.0: lw_cc_surcharge_dry_run now gates the CARD FEE
        # as well as the monthly interest. This class settles real fees,
        # so it must ask for live mode explicitly; before 19.0.5.0.0 the
        # card-fee path ignored the flag entirely, which was the defect.
        # Written here, not in the flag write above, because
        # _check_live_mode_requires_income_account refuses an enabled
        # company in live mode with no Service Charge Income Account.
        cls.company.write({
            'lw_cc_surcharge_income_account_id': cls.income_account.id,
            'lw_cc_surcharge_dry_run': False,
        })

        # Product used by the CCS invoice (fallback handled by the engine).
        cls.product = cls.env.ref(
            'lw_cc_surcharge.product_service_charge',
            raise_if_not_found=False,
        )
        if cls.product:
            cls.company.lw_cc_surcharge_product_id = cls.product.id

        # Net 30 term: the only applicable term.
        cls.term_net30 = cls.env['account.payment.term'].create({
            'name': 'LwCc BIN Test Net 30',
            'line_ids': [Command.create({
                'value': 'percent',
                'value_amount': 100.0,
                'nb_days': 30,
            })],
        })
        cls.company.lw_cc_surcharge_applicable_term_ids = cls.term_net30

        cls.today = fields.Date.context_today(cls.env.user)
        cls.partner = cls.env['res.partner'].create({
            'name': 'LwCc BIN Settlement Customer',
            'company_id': cls.company.id,
        })

        # Payment plwccg for _create_payment: a bank journal, an
        # Authorize.Net provider wired to it, and an inbound payment method
        # line whose payment_provider_id matches the provider (that line is
        # how account_payment resolves payment_method_line_id).
        cls.provider_journal = cls.env['account.journal'].create({
            'name': 'LwCc BIN Provider Bank',
            'type': 'bank',
            'code': 'LBPB',
            'company_id': cls.company.id,
        })
        manual_in = cls.env.ref(
            'account.account_payment_method_manual_in',
            raise_if_not_found=False,
        ) or cls.env['account.payment.method'].search([
            ('code', '=', 'manual'),
            ('payment_type', '=', 'inbound'),
        ], limit=1)
        provider = cls.env['payment.provider'].search([
            ('code', '=', 'authorize'),
        ], limit=1)
        if not provider:
            provider = cls.env['payment.provider'].create({
                'name': 'LwCc BIN Test Authorize',
                'code': 'authorize',
                'state': 'test',
                'is_published': True,
            })
        provider.write({
            'state': 'test',
            'journal_id': cls.provider_journal.id,
            # The authorize provider validates its API fields as soon as
            # it leaves 'disabled'; dummies satisfy the constraint (no
            # live call ever runs in these tests).
            'authorize_login': 'TEST-LOGIN',
            'authorize_transaction_key': 'TEST-KEY',
            'authorize_signature_key': 'TEST-SIGNATURE',
        })
        cls.provider = provider
        # Writing the journal onto the provider auto-creates a
        # provider-linked method line; creating a second one would make
        # core _create_payment's `filtered(...).id` hit a multi-recordset
        # (Expected singleton). Only add a line when none exists. The
        # line keeps its DEFAULT account: pinning payment_account_id to
        # the receivable folds both payment lines onto the receivable
        # and poisons reconciliation (probed on pod).
        provider_line = cls.provider_journal.inbound_payment_method_line_ids\
            .filtered(lambda l: l.payment_provider_id == provider)
        if not provider_line:
            cls.env['account.payment.method.line'].create({
                'journal_id': cls.provider_journal.id,
                'name': 'LwCc BIN Authorize In',
                'payment_method_id': manual_in.id,
                'payment_provider_id': provider.id,
            })

        cls.card_method = cls.env['payment.method'].search([
            ('code', '=', 'card'),
        ], limit=1)
        if not cls.card_method:
            cls.card_method = cls.env['payment.method'].create({
                'name': 'Test card',
                'code': 'card',
            })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_invoice(self, partner, amount, term=None, journal=None):
        """Create and post a customer invoice (same shape as base suite)."""
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
        if journal:
            move_vals['journal_id'] = journal.id
        move = self.env['account.move'].create(move_vals)
        move.action_post()
        return move

    def _register_partial_payment(self, invoice, amount):
        """Register (and post) a PARTIAL payment for ``amount`` against
        ``invoice`` via the standard Pay wizard -- the same mechanism
        the module itself uses, so the resulting reconciliation state is
        exactly what production creates (no hand-rolled account
        matching). Leaves ``invoice`` with ``payment_state == 'partial'``
        when ``amount`` is less than the invoice total.
        """
        wizard = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=invoice.ids,
        ).create({
            'amount': amount,
            'journal_id': self.provider_journal.id,
        })
        wizard.action_create_payments()
        return invoice

    def _create_transaction(self, invoice, amount, fee=0.0, reference=None):
        """Create a done card transaction on the Authorize test provider."""
        tx = self.env['payment.transaction'].create({
            'provider_id': self.provider.id,
            'payment_method_id': self.card_method.id,
            'reference': reference or ('LWCC-TX-%s' % self.id()),
            'amount': amount,
            'currency_id': self.company.currency_id.id,
            'partner_id': self.partner.id,
            'invoice_ids': [Command.set(invoice.ids)],
            'state': 'draft',
            'lw_cc_surcharge_fee_amount': fee,
            # Core _create_payment routes its reconcile loop through
            # `operation == source_transaction_id.operation`; without an
            # operation both sides are False and the EMPTY source branch
            # skips reconciliation entirely. Real portal txs carry
            # 'online_direct'.
            'operation': 'online_direct',
        })
        tx.write({'state': 'done'})
        return tx

    def _fee_invoice(self, tx):
        return self.env['account.move'].sudo().search([
            ('ref', '=', 'CCS/%s' % tx.reference),
            ('move_type', '=', 'out_invoice'),
        ], limit=1)

    # ------------------------------------------------------------------
    # One payment settles invoice + same-invoice fee line
    # ------------------------------------------------------------------

    def test_one_payment_settles_invoice_and_fee(self):
        """A done 103.00 tx (100 invoice + 3 fee) settles the ORIGINAL
        invoice, grown by a same-invoice fee line, with ONE payment and
        leaves zero unreconciled receivable lines.

        The revised default: the fee is now a LINE on the SAME
        invoice, not a separate CCS invoice (that becomes the fallback --
        see the hash-locked-journal test below). This is the rewritten
        settlement contract.
        """
        inv = self._create_invoice(self.partner, 100.0, self.term_net30)
        tx = self._create_transaction(
            inv, amount=103.0, fee=3.0, reference='LWCC-GROWN-%s' % self.id(),
        )

        payment = tx._create_payment()

        # Exactly one payment, linked one-to-one, covering invoice + fee.
        self.assertTrue(payment)
        self.assertEqual(tx.payment_id, payment)
        self.assertEqual(
            self.env['account.payment'].search_count(
                [('payment_transaction_id', '=', tx.id)]),
            1,
        )
        self.assertEqual(payment.amount, 103.0)

        # The fee landed as a LINE on the SAME invoice: no separate CCS
        # invoice exists, and the tx's same-invoice marker is stamped.
        self.assertFalse(
            self._fee_invoice(tx),
            "no separate CCS invoice should exist -- the fee is a line "
            "on the source invoice by default",
        )
        self.assertTrue(tx.lw_cc_surcharge_fee_line_id)
        fee_line = tx.lw_cc_surcharge_fee_line_id
        self.assertEqual(fee_line.move_id, inv)
        self.assertAlmostEqual(fee_line.price_subtotal, 3.0, places=2)
        self.assertEqual(
            fee_line.account_id, self.income_account,
            "the fee line must use the SAME income-account resolution "
            "chain as the legacy _create_cc_surcharge_invoice",
        )

        # The grown invoice (100 + 3 fee line = 103) is fully settled by
        # the single payment. Core accepts 'in_payment' (payment
        # registered, awaiting bank confirmation) as the settled state
        # (account_payment test_payment_flows asserts
        # `in ('in_payment', 'paid')`); the reconciliation asserts below
        # pin the accounting substance.
        self.assertAlmostEqual(inv.amount_total, 103.0, places=2)
        self.assertIn(inv.payment_state, ('in_payment', 'paid'))
        self.assertAlmostEqual(inv.amount_residual, 0.0, places=2)

        # No unreconciled receivable line survives on the invoice.
        leftover = inv.line_ids.filtered(lambda l: (
            l.account_id.account_type == 'asset_receivable'
            and not l.reconciled
        ))
        self.assertFalse(
            leftover, "unreconciled receivable lines remain on the invoice",
        )

        # The payment's receivable (destination) line is fully reconciled:
        # no orphan credit stays open on the journal.
        payment_receivable = payment.move_id.line_ids.filtered(
            lambda l: l.account_id == payment.destination_account_id,
        )
        self.assertEqual(len(payment_receivable), 1)
        self.assertTrue(
            payment_receivable.reconciled,
            "the payment receivable line must be fully reconciled",
        )

    # ------------------------------------------------------------------
    # Nothing is booked before the payment
    # ------------------------------------------------------------------

    def test_no_fee_invoice_before_payment(self):
        """Until _create_payment runs, no CCS invoice and no payment exist
        for the transaction (fee is booked on success only)."""
        inv = self._create_invoice(self.partner, 100.0, self.term_net30)
        tx = self._create_transaction(
            inv, amount=103.0, fee=3.0, reference='LWCC-NOCCS-%s' % self.id(),
        )

        self.assertFalse(self._fee_invoice(tx))
        self.assertFalse(tx.payment_id)
        self.assertEqual(
            self.env['account.payment'].search_count(
                [('payment_transaction_id', '=', tx.id)]),
            0,
        )

    # ------------------------------------------------------------------
    # Legacy engine guard
    # ------------------------------------------------------------------

    def test_legacy_engine_guards(self):
        """Flag off: the legacy post-payment engine still runs (30.0 on a
        1000.0 Net 30 payment). With a fee already charged, or with the
        uplift armed on an invoice-linked tx, _assess returns early."""
        # 1. Flag off + no fee field: legacy behavior unchanged.
        self.company.lw_cc_surcharge_portal_uplift = False
        inv1 = self._create_invoice(self.partner, 1000.0, self.term_net30)
        tx1 = self._create_transaction(
            inv1, amount=1000.0, reference='LWCC-LEGACY-A-%s' % self.id(),
        )
        tx1._assess_cc_surcharge()
        self.assertTrue(tx1.lw_cc_surcharge_invoice_id)
        self.assertAlmostEqual(tx1.lw_cc_surcharge_amount, 30.0, places=2)

        # 2. Fee already charged by the uplift: no second (legacy) invoice.
        inv2 = self._create_invoice(self.partner, 100.0, self.term_net30)
        tx2 = self._create_transaction(
            inv2, amount=103.0, fee=3.0, reference='LWCC-LEGACY-B-%s' % self.id(),
        )
        tx2._assess_cc_surcharge()
        self.assertFalse(tx2.lw_cc_surcharge_invoice_id)

        # 3. Uplift armed + invoice-linked tx without fee: the uplift owns
        # the transaction, _assess defers (no legacy invoice either).
        self.company.lw_cc_surcharge_portal_uplift = True
        inv3 = self._create_invoice(self.partner, 100.0, self.term_net30)
        tx3 = self._create_transaction(
            inv3, amount=100.0, reference='LWCC-LEGACY-C-%s' % self.id(),
        )
        tx3._assess_cc_surcharge()
        self.assertFalse(tx3.lw_cc_surcharge_invoice_id)

    # ------------------------------------------------------------------
    # Idempotency: two calls, one artifact
    # ------------------------------------------------------------------

    def test_apply_cc_fee_idempotent_two_calls_one_artifact(self):
        """Invoking _lw_cc_apply_cc_fee twice for the same transaction
        produces exactly ONE fee artifact (a same-invoice line), never
        two -- the second call is a pure no-op via the fee-line marker."""
        inv = self._create_invoice(self.partner, 100.0, self.term_net30)
        tx = self._create_transaction(
            inv, amount=103.0, fee=3.0,
            reference='LWCC-IDEMP-%s' % self.id(),
        )

        first = tx._lw_cc_apply_cc_fee()
        second = tx._lw_cc_apply_cc_fee()

        self.assertEqual(first, second)
        self.assertEqual(tx.lw_cc_surcharge_fee_line_id, first)
        # Exactly one line matching the fee marker's id on the invoice --
        # not two.
        fee_lines = inv.line_ids.filtered(
            lambda l: l.id == tx.lw_cc_surcharge_fee_line_id.id
        )
        self.assertEqual(len(fee_lines), 1)
        self.assertAlmostEqual(inv.amount_total, 103.0, places=2)
        self.assertFalse(self._fee_invoice(tx))

    # ------------------------------------------------------------------
    # The double-charge regression the savepoint fix closes
    # ------------------------------------------------------------------

    def test_reconcile_failure_rolls_back_never_double_charges(self):
        """THE double-charge regression the savepoint fix in
        _lw_cc_apply_cc_fee closes: if the base helper's re-reconcile
        step raises AFTER the fee line was already written, the write
        must roll back completely -- the invoice must carry NO fee line,
        and exactly ONE fallback CCS invoice must exist. Before the
        savepoint fix, BOTH existed: the invoice kept the already-written
        fee line AND a separate CCS invoice was created on top of it,
        double-billing the customer.

        A pre-existing PARTIAL payment is required to make
        _lw_cc_add_charge_line's reconcile_snapshot non-empty --
        otherwise its re-reconcile loop body never runs at all and there
        is nothing for a patched ``.reconcile()`` to fail on.
        """
        inv = self._create_invoice(self.partner, 100.0, self.term_net30)
        self._register_partial_payment(inv, 40.0)
        self.assertEqual(inv.payment_state, 'partial')

        tx = self._create_transaction(
            inv, amount=63.0, fee=3.0,
            reference='LWCC-ROLLBACK-%s' % self.id(),
        )

        with patch.object(
            type(self.env['account.move.line']), 'reconcile',
            side_effect=UserError('forced re-reconcile failure (test)'),
        ), mute_logger(
            'odoo.addons.lw_cc_surcharge.models.payment_transaction',
        ):
            result = tx._lw_cc_apply_cc_fee()

        # Assert the ABSENCE half first -- this is what the bug got wrong.
        self.assertFalse(
            tx.lw_cc_surcharge_fee_line_id,
            "no same-invoice fee-line marker may survive a rolled-back "
            "write",
        )
        fee_lines_on_invoice = inv.line_ids.filtered(
            lambda l: (
                l.product_id == self.product
                and l.price_subtotal == 3.0
            )
        )
        self.assertFalse(
            fee_lines_on_invoice,
            "the rolled-back fee line must NOT persist on the invoice",
        )
        self.assertAlmostEqual(
            inv.amount_total, 100.0, places=2,
            msg="the invoice total must be back to its ORIGINAL amount "
                "after the savepoint rollback",
        )

        # Assert the PRESENCE half: exactly one fallback invoice exists.
        fee_invoice = self._fee_invoice(tx)
        self.assertTrue(fee_invoice, "the fallback CCS invoice must exist")
        self.assertEqual(result, fee_invoice)
        self.assertAlmostEqual(fee_invoice.amount_total, 3.0, places=2)
        self.assertEqual(
            self.env['account.move'].sudo().search_count([
                ('ref', '=', 'CCS/%s' % tx.reference),
            ]),
            1,
            "exactly one fallback invoice must exist, never two",
        )

    # ------------------------------------------------------------------
    # Partial-paid invoice: allow_partial survives pre-existing reconcile
    # ------------------------------------------------------------------

    def test_fee_line_survives_pre_existing_partial_payment(self):
        """allow_partial=True lets the fee line land on an invoice
        already partially paid by an EARLIER, unrelated payment -- and
        that earlier payment's reconciliation survives the fee-line
        write. This pins the riskiest mechanism in the build
        (_lw_cc_add_charge_line's reconcile_snapshot / re-reconcile)."""
        inv = self._create_invoice(self.partner, 100.0, self.term_net30)
        self._register_partial_payment(inv, 40.0)
        self.assertEqual(inv.payment_state, 'partial')
        self.assertAlmostEqual(inv.amount_residual, 60.0, places=2)

        tx = self._create_transaction(
            inv, amount=63.0, fee=3.0,
            reference='LWCC-PARTIAL-%s' % self.id(),
        )
        fee_line = tx._lw_cc_apply_cc_fee()

        self.assertEqual(fee_line._name, 'account.move.line')
        self.assertEqual(fee_line.move_id, inv)
        self.assertAlmostEqual(inv.amount_total, 103.0, places=2)
        # The EARLIER 40.00 payment is STILL reconciled: residual is now
        # 63.00 (103 grown total - 40 already paid), not the invoice's
        # full new total.
        self.assertAlmostEqual(inv.amount_residual, 63.0, places=2)

        receivable_lines = inv.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable'
        )
        reconciled_partials = (
            receivable_lines.mapped('matched_credit_ids')
            | receivable_lines.mapped('matched_debit_ids')
        )
        self.assertTrue(
            reconciled_partials,
            "the pre-existing 40.00 payment's reconciliation must "
            "survive the fee-line write",
        )

    # ------------------------------------------------------------------
    # Hash-locked journal: falls back to the separate CCS invoice
    # ------------------------------------------------------------------

    def test_hash_locked_journal_falls_back_to_ccs_invoice(self):
        """A hash-locked journal blocks the same-invoice write; the fee
        still gets applied, via the separate CCS invoice fallback -- a
        charged fee is never stranded.

        Uses a DEDICATED test journal (never shared with other tests),
        posts the invoice BEFORE arming the hash lock, and always
        restores the flag afterward -- mirrors the proven pattern in
        lw_cc_surcharge/tests/test_service_charge.py
        (test_07_hash_locked_journal_skipped_and_counted): core refuses
        to unlock a journal that has any move carrying an inalterable
        hash, so a shared/production journal is never safe to toggle.
        """
        test_journal = self.env['account.journal'].create({
            'name': 'LwCc BIN Hash-Locked Sales',
            'code': 'LBPHL',
            'type': 'sale',
            'company_id': self.company.id,
        })
        inv = self._create_invoice(
            self.partner, 100.0, self.term_net30, journal=test_journal,
        )
        tx = self._create_transaction(
            inv, amount=103.0, fee=3.0,
            reference='LWCC-HASHLOCK-%s' % self.id(),
        )

        test_journal.restrict_mode_hash_table = True
        try:
            with mute_logger(
                'odoo.addons.lw_cc_surcharge.models.payment_transaction',
            ):
                result = tx._lw_cc_apply_cc_fee()
        finally:
            # Safe: this invoice posted BEFORE the hash flag was set, so
            # it never carries an inalterable hash; the journal is also
            # brand new and used by nothing else in this test.
            test_journal.restrict_mode_hash_table = False

        self.assertFalse(tx.lw_cc_surcharge_fee_line_id)
        fee_invoice = self._fee_invoice(tx)
        self.assertTrue(fee_invoice)
        self.assertEqual(result, fee_invoice)
        self.assertAlmostEqual(fee_invoice.amount_total, 3.0, places=2)
        # The original invoice is untouched: no fee line was added to it.
        self.assertAlmostEqual(inv.amount_total, 100.0, places=2)

    # ------------------------------------------------------------------
    # Multi-invoice overdue batch: fee on the oldest invoice
    # ------------------------------------------------------------------

    def test_overdue_batch_fee_lands_on_oldest_invoice(self):
        """A tx linked to a multi-invoice overdue batch puts the fee on
        the OLDEST invoice by due date (tie-break on id when two
        invoices share a due date)."""
        newer = self._create_invoice(self.partner, 100.0, self.term_net30)
        newer.invoice_date_due = self.today + timedelta(days=10)
        older = self._create_invoice(self.partner, 50.0, self.term_net30)
        older.invoice_date_due = self.today - timedelta(days=5)
        # Sanity pin (adverse review): writing invoice_date_due on
        # an ALREADY-POSTED invoice is the exact recompute hazard
        # test_uplift_recompute.py documents at length ("action_post
        # recomputes invoice_date_due from the payment term, which
        # would undo a pre-post write") -- these writes happen AFTER
        # _create_invoice's own action_post() call, so no further
        # recompute should touch them, but re-reading and asserting
        # here turns a silent revert into a clear "fixture invalid"
        # failure instead of a confusing behavioral one below.
        self.assertEqual(newer.invoice_date_due, self.today + timedelta(days=10))
        self.assertEqual(older.invoice_date_due, self.today - timedelta(days=5))

        tx = self._create_transaction(
            newer + older, amount=154.5, fee=4.5,
            reference='LWCC-BATCH-%s' % self.id(),
        )
        fee_line = tx._lw_cc_apply_cc_fee()

        self.assertEqual(
            fee_line.move_id, older,
            "the OLDEST invoice by due date must carry the fee",
        )
        self.assertFalse(
            newer.line_ids.filtered(lambda l: l.id == fee_line.id),
            "the newer invoice must not carry the fee line",
        )

        # Tie-break: two invoices sharing the SAME due date -> lower id
        # wins (stable, ordering-independent choice).
        tied_a = self._create_invoice(self.partner, 60.0, self.term_net30)
        tied_a.invoice_date_due = self.today
        tied_b = self._create_invoice(self.partner, 40.0, self.term_net30)
        tied_b.invoice_date_due = self.today
        self.assertEqual(tied_a.invoice_date_due, self.today)
        self.assertEqual(tied_b.invoice_date_due, self.today)
        self.assertLess(tied_a.id, tied_b.id, "fixture sanity for the tie-break")

        tx2 = self._create_transaction(
            tied_a + tied_b, amount=103.0, fee=3.0,
            reference='LWCC-TIE-%s' % self.id(),
        )
        fee_line2 = tx2._lw_cc_apply_cc_fee()
        self.assertEqual(
            fee_line2.move_id, tied_a,
            "the lower id must win the due-date tie-break",
        )

    # ------------------------------------------------------------------
    # : the fee line's label must state the BASE, not the total
    # ------------------------------------------------------------------

    def test_fee_line_label_states_correct_base(self):
        """The fee line's label must state the BASE the percentage was
        applied to (100.00), not the charged total (103.00) -- 3% of
        103 is 3.09, not the 3.00 actually charged. Printing the
        charged total as if it were the base contradicts the fee's own
        arithmetic on the customer's own posted invoice (adverse review
        )."""
        inv = self._create_invoice(self.partner, 100.0, self.term_net30)
        tx = self._create_transaction(
            inv, amount=103.0, fee=3.0,
            reference='LWCC-LABEL-%s' % self.id(),
        )
        fee_line = tx._lw_cc_apply_cc_fee()

        self.assertEqual(fee_line._name, 'account.move.line')
        self.assertIn(
            '100.00', fee_line.name,
            "the label must state the base (100.00) the percentage "
            "was applied to",
        )
        self.assertNotIn(
            '103.00', fee_line.name,
            "the label must NOT state the charged total (103.00) as "
            "if it were the base",
        )

    # ------------------------------------------------------------------
    # Currency rounding: _lw_cc_compute_portal_fee rounds by the
    # currency it is given, not always the company's -- adverse review.
    # ------------------------------------------------------------------

    def test_compute_portal_fee_rounds_by_passed_currency(self):
        """_lw_cc_compute_portal_fee rounds by the CURRENCY PARAMETER
        when one is given, not unconditionally company.currency_id.
        Backward compatible: omitting ``currency`` still rounds by
        company.currency_id (the existing portal-controller call sites,
        which this pass does not touch, rely on exactly that default)."""
        Currency = self.env['res.currency']
        zero_decimal = Currency.search([('name', '=', 'JPY')], limit=1)
        if not zero_decimal:
            zero_decimal = Currency.create({
                'name': 'XTZ',
                'symbol': 'X0',
                'rounding': 1.0,
                'decimal_places': 0,
            })
        if not zero_decimal.active:
            zero_decimal.active = True
        self.assertNotEqual(
            zero_decimal.rounding, self.company.currency_id.rounding,
            "fixture sanity: need a rounding increment that actually "
            "differs from the company currency's",
        )

        Tx = self.env['payment.transaction']
        base = 250.0  # 3% of 250.0 = 7.5 exactly -- unambiguous at
                      # 0.01 precision (stays 7.5) but MUST round away
                      # from 7.5 at a 1.0 (whole-number) precision,
                      # regardless of which half-rounding convention
                      # float_round uses.
        pct = self.company.lw_cc_surcharge_cc_pct

        default_fee = Tx._lw_cc_compute_portal_fee(base, self.company)
        expected_default = float_round(
            base * (pct / 100.0),
            precision_rounding=self.company.currency_id.rounding,
        )
        self.assertAlmostEqual(default_fee, expected_default, places=6)

        fx_fee = Tx._lw_cc_compute_portal_fee(
            base, self.company, currency=zero_decimal,
        )
        expected_fx = float_round(
            base * (pct / 100.0), precision_rounding=zero_decimal.rounding,
        )
        self.assertAlmostEqual(fx_fee, expected_fx, places=6)
        self.assertNotAlmostEqual(
            default_fee, fx_fee, places=2,
            msg="the two roundings must actually diverge for this "
                "test to mean anything",
        )

    # ------------------------------------------------------------------
    # : lw_cc_fee_line is the authoritative marker
    # ------------------------------------------------------------------

    def test_cc_fee_line_marker_authoritative(self):
        """lw_cc_fee_line is the AUTHORITATIVE marker for a CC
        surcharge fee line (adverse review): True on the
        same-invoice fee line, True on the fallback CCS invoice's own
        line, and False on an ordinary invoice line that happens to
        share the same account/product family. Never identify a CC fee
        line by product -- it is shared with the Charge Terms Interest
        / service charge product."""
        inv = self._create_invoice(self.partner, 100.0, self.term_net30)
        tx = self._create_transaction(
            inv, amount=103.0, fee=3.0,
            reference='LWCC-MARKER-%s' % self.id(),
        )
        fee_line = tx._lw_cc_apply_cc_fee()
        self.assertEqual(fee_line._name, 'account.move.line')
        self.assertTrue(fee_line.lw_cc_fee_line)

        ordinary_line = inv.invoice_line_ids - fee_line
        self.assertTrue(ordinary_line)
        self.assertFalse(
            any(ordinary_line.mapped('lw_cc_fee_line')),
            "an ordinary invoice line must not be marked, even though "
            "it may share the same account/product family",
        )

        # The fallback CCS invoice's own line is ALSO marked -- this
        # module can reach it (write a plain field on the returned
        # recordset) without editing lw_cc_surcharge, which owns
        # _create_cc_surcharge_invoice and cannot be extended with
        # extra_line_vals from here.
        hash_locked_journal = self.env['account.journal'].create({
            'name': 'LwCc BIN Marker Hash-Locked Sales',
            'code': 'LBPMK',
            'type': 'sale',
            'company_id': self.company.id,
        })
        inv2 = self._create_invoice(
            self.partner, 100.0, self.term_net30, journal=hash_locked_journal,
        )
        tx2 = self._create_transaction(
            inv2, amount=103.0, fee=3.0,
            reference='LWCC-MARKERFALLBACK-%s' % self.id(),
        )
        hash_locked_journal.restrict_mode_hash_table = True
        try:
            with mute_logger(
                'odoo.addons.lw_cc_surcharge.models.payment_transaction',
            ):
                fallback_invoice = tx2._lw_cc_apply_cc_fee()
        finally:
            hash_locked_journal.restrict_mode_hash_table = False

        self.assertEqual(fallback_invoice._name, 'account.move')
        fallback_lines = fallback_invoice.invoice_line_ids
        self.assertEqual(len(fallback_lines), 1)
        self.assertTrue(fallback_lines.lw_cc_fee_line)
