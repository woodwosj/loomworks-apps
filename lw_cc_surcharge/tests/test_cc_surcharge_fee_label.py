# -*- coding: utf-8 -*-
"""Regression test (adverse review): the CC
surcharge charge line's ``name`` and the invoice's ``narration`` used
to interpolate ``transaction.amount`` as "the base the percentage was
applied to" -- correct for ONE caller of the shared
``account.move._create_cc_surcharge_invoice`` and wrong for the other:

  - The legacy engine's own ``_assess_cc_surcharge``
    (lw_cc_surcharge/models/payment_transaction.py) computes
    ``surcharge_amount = self.amount * (cc_pct / 100.0)`` where
    ``self.amount`` genuinely IS the base (the fee is a separate
    invoice, never combined into the transaction amount). The text
    was correct in that context.
  - lw_cc_surcharge's fallback path
    (``_lw_cc_fallback_cc_fee_invoice``) calls this SAME shared method
    with a transaction whose ``.amount`` is base+fee under the portal
    uplift (the card was already charged for the combined total). The
    text then stated arithmetic that contradicts itself: e.g.
    "3.00% of 1030.00" on a line priced 30.00, when 3% of 1030 is
    30.90, not 30.00.

Fix: the displayed base is now recovered directly from
``surcharge_amount`` and ``pct`` (``base = surcharge_amount /
(pct/100)``) -- the exact inverse of how BOTH callers compute the fee
in the first place (verified by reading both computations directly;
see account_move.py's ``_create_cc_surcharge_invoice`` comment for the
citations) -- and never reads ``transaction.amount`` at all in the
normal (pct > 0) path. This test proves that property directly: it
calls the shared method TWICE with the IDENTICAL surcharge_amount/pct
but DIFFERENT transaction.amount values -- one shaped like the legacy
caller's semantics (amount == base), one shaped like the fallback
caller's (amount == base + fee) -- and asserts the displayed base is
IDENTICAL and CORRECT in both calls, not silently wrong for one of
them.

This is a NEW, separate test file (not an addition to
test_cc_surcharge.py, which another agent owns concurrently) and
exercises ONLY base-module code -- no dependency on
lw_cc_surcharge. It creates a plain ``payment.transaction``
(a core model from the ``payment`` module, already a dependency) and
sets its ``.amount`` directly to whatever shape each scenario needs;
it does not need the fallback code path to exist to prove
the shared method's output is caller-agnostic. The actual fallback
code path is exercised end-to-end by this module's settlement
tests instead.
"""
from odoo import Command, fields
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'lw_cc_surcharge')
class TestCCSurchargeFeeLabel(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.company = cls.env.company
        cls.company.write({
            'lw_cc_surcharge_enabled': True,
            'lw_cc_surcharge_cc_pct': 3.0,
        })

        Account = cls.env['account.account'].with_context(
            allowed_company_ids=cls.company.ids,
        )
        cls.income_account = Account.search([
            ('account_type', '=', 'income'),
        ], limit=1)
        if not cls.income_account:
            cls.income_account = Account.create({
                'name': 'Test CC Fee Label Income',
                'code': '999902',
                'account_type': 'income',
            })
        cls.company.lw_cc_surcharge_cc_income_account_id = (
            cls.income_account.id
        )

        cls.product = cls.env.ref(
            'lw_cc_surcharge.product_service_charge',
            raise_if_not_found=False,
        )
        if cls.product:
            cls.company.lw_cc_surcharge_product_id = cls.product.id

        cls.today = fields.Date.context_today(cls.env.user)

        cls.partner = cls.env['res.partner'].create({
            'name': 'CC Fee Label Test Customer',
            'company_id': cls.company.id,
        })

        # Minimal source invoice -- only used for invoice_origin/chatter;
        # its amount is unrelated to the fee math under test.
        cls.source_invoice = cls.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': cls.partner.id,
            'invoice_line_ids': [Command.create({
                'name': 'Test Product',
                'quantity': 1,
                'price_unit': 1000.0,
                'account_id': cls.income_account.id,
            })],
        })
        cls.source_invoice.action_post()

        # Provider + payment method, mirroring test_cc_surcharge.py's
        # own _create_transaction helper.
        cls.provider = cls.env['payment.provider'].search([
            ('code', '=', 'authorize'),
        ], limit=1)
        if not cls.provider:
            cls.provider = cls.env['payment.provider'].create({
                'name': 'Test Authorize.Net (Fee Label)',
                'code': 'authorize',
                'state': 'enabled',
                'is_published': True,
            })
        cls.payment_method = cls.env['payment.method'].search([
            ('code', '=', 'card'),
        ], limit=1)
        if not cls.payment_method:
            cls.payment_method = cls.env['payment.method'].create({
                'name': 'Test card (Fee Label)',
                'code': 'card',
            })

    def _create_transaction(self, ref_suffix, amount):
        """A minimal done payment.transaction with a caller-chosen
        ``.amount`` -- the ONE thing this test needs to vary between
        the legacy-shaped and fallback-shaped scenarios.
        """
        tx = self.env['payment.transaction'].create({
            'provider_id': self.provider.id,
            'provider_code': 'authorize',
            'payment_method_id': self.payment_method.id,
            'reference': 'TEST-FEE-LABEL-%s-%s' % (self.id(), ref_suffix),
            'amount': amount,
            'currency_id': self.company.currency_id.id,
            'partner_id': self.partner.id,
            'invoice_ids': [Command.set(self.source_invoice.ids)],
            'state': 'draft',
        })
        tx.write({'state': 'done'})
        return tx

    def test_01_label_base_is_caller_agnostic_and_correct(self):
        """The SAME surcharge_amount/pct, with two DIFFERENT
        transaction.amount values (one legacy-shaped, one
        fallback-shaped), must produce the SAME correct displayed
        base in both the line name and the narration.

        pct = 3.0%, surcharge_amount = 30.00 -> the only
        arithmetically consistent base is 1000.00 (30.00 / 0.03).
        Pre-fix, the legacy-shaped call (transaction.amount = 1000.00,
        i.e. == base) happened to read correctly by coincidence, while
        the fallback-shaped call (transaction.amount = 1030.00, i.e.
        base + fee) would have printed "3.00% of 1030.00" on a line
        priced 30.00 -- arithmetic that contradicts itself, since 3%
        of 1030 is 30.90, not 30.00.
        """
        pct = 3.0
        surcharge_amount = 30.0
        expected_base_str = '1000.00'  # 30.00 / (3.0/100)

        # Legacy-shaped: transaction.amount == base (no fee combined).
        tx_legacy = self._create_transaction('LEGACY', 1000.0)
        inv_legacy = self.env['account.move'].sudo()._create_cc_surcharge_invoice(
            partner=self.partner,
            surcharge_amount=surcharge_amount,
            company=self.company,
            source_invoices=self.source_invoice,
            transaction=tx_legacy,
        )
        self.assertTrue(inv_legacy)
        self.assertIn(
            '%.2f%% of %s' % (pct, expected_base_str),
            inv_legacy.invoice_line_ids.name,
        )
        self.assertIn(
            '%.2f%% of %s' % (pct, expected_base_str),
            inv_legacy.narration,
        )

        # Fallback-shaped: transaction.amount == base + fee (1030.00).
        # SAME surcharge_amount/pct as above.
        tx_fallback = self._create_transaction('FALLBACK', 1030.0)
        inv_fallback = self.env['account.move'].sudo()._create_cc_surcharge_invoice(
            partner=self.partner,
            surcharge_amount=surcharge_amount,
            company=self.company,
            source_invoices=self.source_invoice,
            transaction=tx_fallback,
        )
        self.assertTrue(inv_fallback)
        # Must show the SAME base as the legacy-shaped call above --
        # NOT 1030.00 (transaction.amount), and NOT any other value
        # that would make "3.00% of X" arithmetically inconsistent
        # with the 30.00 line price.
        self.assertIn(
            '%.2f%% of %s' % (pct, expected_base_str),
            inv_fallback.invoice_line_ids.name,
        )
        self.assertIn(
            '%.2f%% of %s' % (pct, expected_base_str),
            inv_fallback.narration,
        )
        self.assertNotIn('1030.00', inv_fallback.invoice_line_ids.name)
        self.assertNotIn('1030.00', inv_fallback.narration)

    def test_02_zero_pct_at_creation_falls_back_to_transaction_amount(self):
        """If cc_pct is 0/unset at THIS moment (a race between when
        the fee was computed and this invoice-creation call), base
        cannot be reconstructed by division -- the documented
        fallback is transaction.amount itself, not a crash.
        """
        self.company.lw_cc_surcharge_cc_pct = 0.0
        tx = self._create_transaction('ZEROPCT', 1234.56)
        inv = self.env['account.move'].sudo()._create_cc_surcharge_invoice(
            partner=self.partner,
            surcharge_amount=30.0,
            company=self.company,
            source_invoices=self.source_invoice,
            transaction=tx,
        )
        self.assertTrue(inv)
        self.assertIn('1234.56', inv.invoice_line_ids.name)

        self.company.lw_cc_surcharge_cc_pct = 3.0  # restore
