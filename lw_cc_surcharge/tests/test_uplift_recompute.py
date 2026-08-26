# -*- coding: utf-8 -*-
"""Server-side recompute tests for the portal CC surcharge uplift
and the flag-off regression.

Drives the real invoice transaction routes with jsonrpc POSTs, as the
payment form does. The server must derive the base from the invoice's
payment menu, never from the client amount:

  - A tampered client amount (1.00 on a $100 invoice) falls back to
    the full residual: the tx is uplifted to 103.00 with fee 3.00.
  - A legit menu amount on a partially paid invoice ($60 paid of
    $100) uplifts the 40.00 installment to 41.20.
  - The token flow uplifts on the token's CREDIT verdict and skips
    tokens without a verdict.
  - The overdue batch (100 + 50 residuals) uplifts 150.00 to 154.50.
  - The session quote verdict is consumed on first use: the second
    transaction on the same invoice is not uplifted.
  - flag-off regression: no quote, no uplift, amount passes through.

No payment_demo: the Authorize provider runs in state 'test'; flow
'direct' only creates the draft transaction (the charge happens later at
the provider controller), and the token flow's immediate charge is
stubbed with a patch (nothing here asserts on provider I/O).
"""
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.tests import HttpCase, tagged
from odoo.tools import mute_logger


@tagged('post_install', '-at_install', 'lw_cc_surcharge')
class TestPortalUpliftRecompute(HttpCase):

    def setUp(self):
        super().setUp()
        self.company = self.env.company
        self.company.sudo().write({
            'lw_cc_surcharge_enabled': True,
            'lw_cc_surcharge_cc_pct': 3.0,
            'lw_cc_surcharge_portal_uplift': True,
        })

        Account = self.env['account.account'].with_context(
            allowed_company_ids=self.company.ids,
        )
        self.income_account = Account.search([
            ('account_type', '=', 'income'),
        ], limit=1)
        if not self.income_account:
            self.income_account = Account.create({
                'name': 'Test CC Surcharge Income',
                'code': '999901',
                'account_type': 'income',
            })

        # 19.0.5.0.0: lw_cc_surcharge_dry_run now gates the CARD FEE
        # as well as the monthly interest. This class uplifts real
        # transaction amounts, which is precisely what dry-run now
        # suppresses, so it must ask for live mode explicitly. Written
        # here, not in the flag write above, because
        # _check_live_mode_requires_income_account refuses an enabled
        # company in live mode with no Service Charge Income Account.
        self.company.sudo().write({
            'lw_cc_surcharge_income_account_id': self.income_account.id,
            'lw_cc_surcharge_dry_run': False,
        })

        self.term_net30 = self.env['account.payment.term'].create({
            'name': 'LwCc BIN Uplift Net 30',
            'line_ids': [Command.create({
                'value': 'percent',
                'value_amount': 100.0,
                'nb_days': 30,
            })],
        })
        self.company.sudo().lw_cc_surcharge_applicable_term_ids = (
            self.term_net30
        )

        self.today = fields.Date.context_today(self.env.user)

        # Portal user paying their own invoices (token ownership and the
        # session verdict key both resolve through this partner).
        self.portal_login = 'lwcc_portal_tx'
        self.portal_user = self.env['res.users'].create({
            'name': 'LwCc BIN Portal Payer',
            'login': self.portal_login,
            'password': 'LumbinPortal1!',
            'group_ids': [Command.set(
                self.env.ref('base.group_portal').ids,
            )],
        })
        self.partner = self.portal_user.partner_id

        # Payment plwccg: provider journal (with the authorize inbound
        # line) and a separate manual journal for the partial payment.
        # Method lines keep their DEFAULT accounts: setting
        # payment_account_id to the receivable folds both payment lines
        # onto the receivable and breaks reconciliation (probed on pod:
        # with invoice_ids the payment's counterpart comes from the
        # invoice receivable; the method line account must stay a
        # suspense/outstanding-side default).
        # v19 made the chart of accounts company-independent: no
        # company_id field on account.account anymore.
        self.provider_journal = self.env['account.journal'].create({
            'name': 'LwCc BIN Uplift Provider Bank',
            'type': 'bank',
            'code': 'LBPK',
            'company_id': self.company.id,
        })
        self.manual_journal = self.env['account.journal'].create({
            'name': 'LwCc BIN Uplift Manual Bank',
            'type': 'bank',
            'code': 'LBPM',
            'company_id': self.company.id,
        })
        manual_in = self.env.ref(
            'account.account_payment_method_manual_in',
            raise_if_not_found=False,
        ) or self.env['account.payment.method'].search([
            ('code', '=', 'manual'),
            ('payment_type', '=', 'inbound'),
        ], limit=1)
        provider = self.env['payment.provider'].search([
            ('code', '=', 'authorize'),
        ], limit=1)
        if not provider:
            provider = self.env['payment.provider'].create({
                'name': 'LwCc BIN Uplift Authorize',
                'code': 'authorize',
                'state': 'test',
                'is_published': True,
            })
        provider.write({
            'state': 'test',
            'journal_id': self.provider_journal.id,
            # Dummies satisfy the authorize API-field constraint (state
            # 'test' still validates them); no live call runs here.
            'authorize_login': 'TEST-LOGIN',
            'authorize_transaction_key': 'TEST-KEY',
            'authorize_signature_key': 'TEST-SIGNATURE',
        })
        self.provider = provider
        # Only add a provider-linked line when the journal write did not
        # auto-create one: two provider-linked lines would break core
        # _create_payment's singleton `.id` on the filtered lines.
        provider_line = self.provider_journal.inbound_payment_method_line_ids\
            .filtered(lambda l: l.payment_provider_id == provider)
        if not provider_line:
            provider_line = self.env['account.payment.method.line'].create({
                'journal_id': self.provider_journal.id,
                'name': 'LwCc BIN Uplift Authorize In',
                'payment_method_id': manual_in.id,
                'payment_provider_id': provider.id,
            })
        self.manual_line = self.env['account.payment.method.line'].create({
            'journal_id': self.manual_journal.id,
            'name': 'LwCc BIN Uplift Manual In',
            'payment_method_id': manual_in.id,
        })

        self.card_method = self.env['payment.method'].search([
            ('code', '=', 'card'),
        ], limit=1)
        if not self.card_method:
            self.card_method = self.env['payment.method'].create({
                'name': 'Test card',
                'code': 'card',
            })

        # Private CREDIT range so the fixture is hermetic.
        self.env['lw_cc.bin.record'].sudo().search([
            ('bin_start', '<=', '555559'),
            ('bin_end', '>=', '555550'),
        ]).unlink()
        self.env['lw_cc.bin.record'].create({
            'bin_start': '555550',
            'bin_end': '555559',
            'card_type': 'CREDIT',
            'network': 'VISA',
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_invoice(self, amount, term=None, due_offset_days=None):
        """Create and post a customer invoice for the portal partner."""
        move_vals = {
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_date': self.today - timedelta(days=1),
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
        if due_offset_days is not None:
            # Stamp the due date AFTER posting: action_post recomputes
            # invoice_date_due from the payment term, which would undo a
            # pre-post write and leave the invoice NOT past due.
            move.invoice_date_due = self.today + timedelta(
                days=due_offset_days,
            )
        return move

    def _jsonrpc(self, url, params):
        resp = self.url_open(url, json={
            'jsonrpc': '2.0',
            'method': 'call',
            'id': 1,
            'params': params,
        })
        self.assertEqual(resp.status_code, 200, resp.content[:1000])
        data = json.loads(resp.content)
        if data.get('error'):
            self.fail('jsonrpc error on %s: %s' % (url, data['error']))
        return data.get('result')

    def _authenticate_portal(self):
        """Login as the portal payer; the opener keeps the session for the
        quote (verdict seed) and the transaction call (verdict consume)."""
        self.authenticate(self.portal_login, 'LumbinPortal1!')

    def _quote(self, invoice, card_bin='555555', amount=None, **extra):
        params = {
            'invoice_id': invoice.id,
            'access_token': invoice._portal_ensure_token(),
            'card_bin': card_bin,
        }
        if amount is not None:
            params['amount'] = amount
        params.update(extra)
        return self._jsonrpc('/lw_cc_surcharge/quote', params)

    def _invoice_tx(self, invoice, amount, flow='direct', token_id=None):
        """POST /invoice/transaction/<id> with the whitelisted kwargs the
        payment form sends; returns the processing values."""
        return self._jsonrpc('/invoice/transaction/%s' % invoice.id, {
            'access_token': invoice._portal_ensure_token(),
            'provider_id': self.provider.id,
            'payment_method_id': self.card_method.id,
            'token_id': token_id,
            'amount': amount,
            'flow': flow,
            'tokenization_requested': False,
            'landing_route': '/my/invoices',
        })

    def _overdue_tx(self, amount, payment_reference):
        return self._jsonrpc('/invoice/transaction/overdue', {
            'payment_reference': payment_reference,
            'provider_id': self.provider.id,
            'payment_method_id': self.card_method.id,
            'token_id': None,
            'amount': amount,
            'flow': 'direct',
            'tokenization_requested': False,
            'landing_route': '/my/invoices',
        })

    def _tx(self, processing_values):
        """Fetch the created transaction by its reference."""
        tx = self.env['payment.transaction'].sudo().search([
            ('reference', '=', processing_values['reference']),
        ], limit=1)
        self.assertTrue(tx, "the transaction must have been created")
        return tx

    # ------------------------------------------------------------------
    # Tampered client amount falls back to the residual
    # ------------------------------------------------------------------

    def test_tampered_amount_falls_back_to_residual(self):
        inv = self._create_invoice(100.0, self.term_net30)
        self._authenticate_portal()

        quote = self._quote(inv, amount=100.0)
        self.assertEqual(quote.get('applies'), True, msg=str(quote))

        with mute_logger('odoo.addons.payment.models.payment_transaction'):
            result = self._invoice_tx(inv, 1.00)
        tx = self._tx(result)

        # 1.00 is not on the payment menu: the base fell back to the full
        # residual (100.00) and the charged amount was uplifted.
        self.assertAlmostEqual(tx.amount, 103.0, places=2)
        self.assertAlmostEqual(tx.lw_cc_surcharge_fee_amount, 3.0, places=2)
        self.assertEqual(tx.lw_cc_surcharge_bin_check, 'CREDIT')
        self.assertEqual(tx.invoice_ids, inv)

    # ------------------------------------------------------------------
    # Legit menu amount on a partially paid invoice
    # ------------------------------------------------------------------

    def test_partial_payment_next_amount_uplifted(self):
        # Two-installment term (40 now, 60 later): the pay menu offers
        # next_amount_to_pay = 40.00 with amount_due = 100.00. Same menu
        # semantics as a partially paid invoice, without any payment
        # plwccg (registering a payment on this DB does not reconcile
        # deterministically; the installment term is the real-world
        # 'installments tab' scenario anyway).
        term_40_60 = self.env['account.payment.term'].create({
            'name': 'LwCc BIN Uplift 40/60',
            'line_ids': [
                Command.create({'value': 'percent', 'value_amount': 40.0, 'nb_days': 15}),
                Command.create({'value': 'percent', 'value_amount': 60.0, 'nb_days': 30}),
            ],
        })
        self.company.sudo().lw_cc_surcharge_applicable_term_ids = (
            self.term_net30 + term_40_60
        )
        inv = self._create_invoice(100.0, term_40_60)

        next_values = inv._get_invoice_next_payment_values()
        self.assertEqual(next_values.get('installment_state'), 'next')
        self.assertAlmostEqual(next_values.get('next_amount_to_pay'), 40.0, places=2)
        self.assertAlmostEqual(next_values.get('amount_due'), 100.0, places=2)

        self._authenticate_portal()
        quote = self._quote(inv, amount=40.0)
        self.assertEqual(quote.get('applies'), True, msg=str(quote))
        self.assertAlmostEqual(quote.get('base'), 40.0, places=2)
        self.assertAlmostEqual(quote.get('total'), 41.2, places=2)

        with mute_logger('odoo.addons.payment.models.payment_transaction'):
            result = self._invoice_tx(inv, 40.00)
        tx = self._tx(result)

        self.assertAlmostEqual(tx.amount, 41.2, places=2)
        self.assertAlmostEqual(tx.lw_cc_surcharge_fee_amount, 1.2, places=2)

    # ------------------------------------------------------------------
    # Token verdict gates the uplift
    # ------------------------------------------------------------------

    def _create_token(self, name, verdict=None):
        vals = {
            'provider_id': self.provider.id,
            'partner_id': self.partner.id,
            'payment_method_id': self.card_method.id,
            'provider_ref': name,
        }
        if verdict:
            vals['lw_cc_surcharge_bin_check'] = verdict
        return self.env['payment.token'].create(vals)

    def test_token_verdict_gates_uplift(self):
        inv = self._create_invoice(100.0, self.term_net30)
        token_credit = self._create_token('LWCC-TOK-CREDIT', 'CREDIT')
        token_plain = self._create_token('LWCC-TOK-PLAIN')
        self._authenticate_portal()

        # Token payments are charged immediately at creation: stub the
        # provider request so no provider I/O happens (nothing here
        # asserts on the charge itself).
        with patch.object(
            type(self.env['payment.transaction']), '_send_payment_request',
        ), mute_logger('odoo.addons.payment.models.payment_transaction'):
            result = self._invoice_tx(
                inv, 100.0, flow='token', token_id=token_credit.id,
            )
        tx = self._tx(result)
        self.assertAlmostEqual(tx.amount, 103.0, places=2)
        self.assertAlmostEqual(tx.lw_cc_surcharge_fee_amount, 3.0, places=2)

        with patch.object(
            type(self.env['payment.transaction']), '_send_payment_request',
        ), mute_logger('odoo.addons.payment.models.payment_transaction'):
            result = self._invoice_tx(
                inv, 100.0, flow='token', token_id=token_plain.id,
            )
        tx_plain = self._tx(result)
        self.assertAlmostEqual(tx_plain.amount, 100.0, places=2)
        self.assertFalse(tx_plain.lw_cc_surcharge_fee_amount)

    # ------------------------------------------------------------------
    # The session verdict is consumed on first use
    # ------------------------------------------------------------------

    def test_session_verdict_consumed_once(self):
        inv = self._create_invoice(100.0, self.term_net30)
        self._authenticate_portal()

        # ONE quote, then TWO transactions: the first consumes the
        # verdict, the second must not be uplifted.
        quote = self._quote(inv, amount=100.0)
        self.assertEqual(quote.get('applies'), True, msg=str(quote))

        with mute_logger('odoo.addons.payment.models.payment_transaction'):
            result1 = self._invoice_tx(inv, 1.00)
            result2 = self._invoice_tx(inv, 100.00)
        tx1 = self._tx(result1)
        tx2 = self._tx(result2)

        self.assertAlmostEqual(tx1.amount, 103.0, places=2)
        self.assertAlmostEqual(tx1.lw_cc_surcharge_fee_amount, 3.0, places=2)
        self.assertAlmostEqual(tx2.amount, 100.0, places=2)
        self.assertFalse(tx2.lw_cc_surcharge_fee_amount)

    # ------------------------------------------------------------------
    # Overdue batch
    # ------------------------------------------------------------------

    def test_overdue_batch_uplift(self):
        inv1 = self._create_invoice(
            100.0, self.term_net30, due_offset_days=-5,
        )
        inv2 = self._create_invoice(
            50.0, self.term_net30, due_offset_days=-5,
        )
        self._authenticate_portal()

        quote = self._jsonrpc('/lw_cc_surcharge/quote', {
            'overdue': True,
            'card_bin': '555555',
        })
        self.assertEqual(quote.get('applies'), True, msg=str(quote))
        self.assertAlmostEqual(quote.get('base'), 150.0, places=2)
        self.assertAlmostEqual(quote.get('total'), 154.5, places=2)

        with mute_logger('odoo.addons.payment.models.payment_transaction'):
            result = self._overdue_tx(150.0, 'LWCC-OD')
        tx = self._tx(result)

        self.assertAlmostEqual(tx.amount, 154.5, places=2)
        self.assertAlmostEqual(tx.lw_cc_surcharge_fee_amount, 4.5, places=2)
        self.assertEqual(set(tx.invoice_ids.ids), {inv1.id, inv2.id})

    # ------------------------------------------------------------------
    # Flag-off regression: byte-identical passthrough
    # ------------------------------------------------------------------

    def test_flag_off_no_quote_no_uplift(self):
        self.company.sudo().lw_cc_surcharge_portal_uplift = False
        inv = self._create_invoice(100.0, self.term_net30)
        self._authenticate_portal()

        quote = self._quote(inv, amount=100.0)
        self.assertEqual(quote.get('applies'), False)
        self.assertEqual(quote.get('reason'), 'gates')

        # No session verdict, CREDIT card data or not: the amount passes
        # through exactly as the client sent it, no fee field is set.
        with mute_logger('odoo.addons.payment.models.payment_transaction'):
            result = self._invoice_tx(inv, 100.00)
        tx = self._tx(result)
        self.assertAlmostEqual(tx.amount, 100.0, places=2)
        self.assertFalse(tx.lw_cc_surcharge_fee_amount)
        self.assertFalse(tx.lw_cc_surcharge_bin_check)
