# -*- coding: utf-8 -*-
"""Quote route tests for the portal CC surcharge uplift.

Drives /lw_cc_surcharge/quote with real jsonrpc POSTs:
  - A bad access token answers {'applies': False, 'reason': 'access'}
    and leaks no other field.
  - A valid token works from a public (unauthenticated) session.
  - A CREDIT bin quotes fee/base/total/pct on a $100 Net 30 invoice.
  - DEBIT and unknown bins are never surcharged (the OLD engine
    surcharged unknown BINs; the quote route must fail closed).
  - Gate misses (term not applicable, flag off, pct 0) answer 'gates';
    an EPD invoice answers 'epd' (direct helper call: patching the payment
    values is deterministic where building a real EPD term fixture is not).
"""
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.tests import HttpCase, tagged
from odoo.tools import mute_logger

from odoo.addons.lw_cc_surcharge.controllers.payment_portal \
    import LwCcSurchargePaymentPortal


@tagged('post_install', '-at_install', 'lw_cc_surcharge')
class TestPortalUpliftQuoteRoute(HttpCase):

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
        # as well as the monthly interest, so a fixture that expects a
        # live fee (or a live QUOTE -- dry-run suppresses quoting too,
        # since quoting a fee the checkout will not charge would show
        # the customer a total that does not match their card) has to
        # ask for live mode explicitly. Before 19.0.5.0.0 the card-fee
        # path ignored the flag entirely, which was the defect.
        #
        # Written here rather than in the flag write above because
        # _check_live_mode_requires_income_account refuses an enabled
        # company in live mode with no Service Charge Income Account.
        self.company.sudo().write({
            'lw_cc_surcharge_income_account_id': self.income_account.id,
            'lw_cc_surcharge_dry_run': False,
        })

        self.term_net30 = self.env['account.payment.term'].create({
            'name': 'LwCc BIN Quote Net 30',
            'line_ids': [Command.create({
                'value': 'percent',
                'value_amount': 100.0,
                'nb_days': 30,
            })],
        })
        self.term_net0 = self.env['account.payment.term'].create({
            'name': 'LwCc BIN Quote Net 0',
            'line_ids': [Command.create({
                'value': 'percent',
                'value_amount': 100.0,
                'nb_days': 0,
            })],
        })
        # Net 30 is the only applicable term: Net 0 misses the term gate.
        self.company.sudo().lw_cc_surcharge_applicable_term_ids = (
            self.term_net30
        )

        self.today = fields.Date.context_today(self.env.user)
        self.partner = self.env['res.partner'].create({
            'name': 'LwCc BIN Quote Customer',
            'company_id': self.company.id,
        })

        # BIN table: private ranges so the fixture is hermetic regardless
        # of whatever ranges exist in the database.
        self._seed_bin('555550', '555559', 'CREDIT')
        self._seed_bin('411110', '411119', 'DEBIT')
        self._clear_bin('999990', '999999')  # the unknown-bin probe range

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _clear_bin(self, start, end):
        """Remove any BIN rows overlapping [start, end] (test contaminants)."""
        self.env['lw_cc.bin.record'].sudo().search([
            ('bin_start', '<=', end),
            ('bin_end', '>=', start),
        ]).unlink()

    def _seed_bin(self, start, end, card_type):
        self._clear_bin(start, end)
        return self.env['lw_cc.bin.record'].create({
            'bin_start': start,
            'bin_end': end,
            'card_type': card_type,
            'network': 'VISA',
        })

    def _create_invoice(self, amount, term=None, partner=None):
        partner = partner or self.partner
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

    def _quote(self, invoice_id, access_token, card_bin, **extra):
        """POST a jsonrpc quote call (no login: public session)."""
        params = {
            'invoice_id': invoice_id,
            'access_token': access_token,
            'card_bin': card_bin,
        }
        params.update(extra)
        resp = self.url_open('/lw_cc_surcharge/quote', json={
            'jsonrpc': '2.0',
            'method': 'call',
            'id': 1,
            'params': params,
        })
        self.assertEqual(resp.status_code, 200, resp.content[:1000])
        data = json.loads(resp.content)
        if data.get('error'):
            self.fail('quote jsonrpc error: %s' % data['error'])
        return data.get('result')

    # ------------------------------------------------------------------
    # Bad access token
    # ------------------------------------------------------------------

    def test_bad_access_token_leaks_nothing(self):
        """A wrong token answers applies/reason only: no amounts, no ids,
        nothing that confirms or denies the document beyond the reason."""
        inv = self._create_invoice(100.0, self.term_net30)
        result = self._quote(inv.id, 'definitely-not-the-token', '555555')

        self.assertEqual(result.get('applies'), False)
        self.assertEqual(result.get('reason'), 'access')
        self.assertTrue(
            set(result) <= {'applies', 'reason'},
            "the access-failure answer must not leak fields: %s" % result,
        )

    # ------------------------------------------------------------------
    # Public session, valid token
    # ------------------------------------------------------------------

    def test_public_session_valid_token(self):
        """auth='public': a valid access token is enough, no login needed."""
        inv = self._create_invoice(100.0, self.term_net30)
        result = self._quote(inv.id, inv._portal_ensure_token(), '555555')

        self.assertEqual(result.get('applies'), True)
        self.assertAlmostEqual(result.get('fee'), 3.0, places=2)

    # ------------------------------------------------------------------
    # CREDIT bin quote amounts
    # ------------------------------------------------------------------

    def test_credit_bin_quote_amounts(self):
        """A CREDIT bin on a $100 Net 30 invoice quotes fee 3.00, base
        100.00, total 103.00, pct 3.0, with the currency symbol."""
        inv = self._create_invoice(100.0, self.term_net30)

        result = self._quote(
            inv.id, inv._portal_ensure_token(), '5555551234', amount=100.0,
        )
        self.assertEqual(result.get('applies'), True)
        self.assertAlmostEqual(result.get('fee'), 3.0, places=2)
        self.assertAlmostEqual(result.get('base'), 100.0, places=2)
        self.assertAlmostEqual(result.get('total'), 103.0, places=2)
        self.assertAlmostEqual(result.get('pct'), 3.0, places=2)
        self.assertTrue(result.get('currency_symbol'))

        # Sanitization: only digits count, only the first 6 are used.
        result = self._quote(
            inv.id, inv._portal_ensure_token(), '55 55-55x999', amount=100.0,
        )
        self.assertEqual(result.get('applies'), True)
        self.assertAlmostEqual(result.get('fee'), 3.0, places=2)

    # ------------------------------------------------------------------
    # DEBIT and unknown bins fail closed
    # ------------------------------------------------------------------

    def test_debit_and_unknown_never_surcharged(self):
        """Posture flip vs the legacy engine: a seeded DEBIT bin answers
        'debit' and an unseeded bin answers 'unknown': never a fee."""
        inv = self._create_invoice(100.0, self.term_net30)

        debit = self._quote(inv.id, inv._portal_ensure_token(), '411111')
        self.assertEqual(debit.get('applies'), False)
        self.assertEqual(debit.get('reason'), 'debit')

        unknown = self._quote(inv.id, inv._portal_ensure_token(), '999999')
        self.assertEqual(unknown.get('applies'), False)
        self.assertEqual(unknown.get('reason'), 'unknown')

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    def test_gate_misses_answer_gates(self):
        """Term not applicable, flag off, and pct 0 all answer 'gates'."""
        # Term not in the applicable list (Net 0).
        inv_net0 = self._create_invoice(100.0, self.term_net0)
        result = self._quote(inv_net0.id, inv_net0._portal_ensure_token(), '555555')
        self.assertEqual(result.get('applies'), False)
        self.assertEqual(result.get('reason'), 'gates')

        # Uplift flag off: everything else still configured.
        inv = self._create_invoice(100.0, self.term_net30)
        token = inv._portal_ensure_token()
        self.company.sudo().lw_cc_surcharge_portal_uplift = False
        result = self._quote(inv.id, token, '555555')
        self.assertEqual(result.get('applies'), False)
        self.assertEqual(result.get('reason'), 'gates')

        # Percentage 0.
        self.company.sudo().lw_cc_surcharge_portal_uplift = True
        self.company.sudo().lw_cc_surcharge_cc_pct = 0.0
        result = self._quote(inv.id, token, '555555')
        self.assertEqual(result.get('applies'), False)
        self.assertEqual(result.get('reason'), 'gates')

    def test_epd_invoices_never_uplifted(self):
        """EPD invoices are excluded (the base _create_payment gates the
        early-payment-discount write-off on an EXACT amount match).

        The epd branch is exercised by calling the gate helper directly
        with _get_invoice_next_payment_values patched: building a real EPD
        term fixture drags in discount configuration that v19 keeps
        changing, while the patched call is deterministic and tests exactly
        the branch this module owns.
        """
        inv = self._create_invoice(100.0, self.term_net30)
        controller = LwCcSurchargePaymentPortal()

        # Control: the same invoice, unpatched, passes the gates.
        self.assertIsNone(controller._lw_cc_quote_gates_fail(inv.sudo()))

        with patch.object(
            type(self.env['account.move']),
            '_get_invoice_next_payment_values',
            return_value={'installment_state': 'epd'},
        ):
            self.assertEqual(
                controller._lw_cc_quote_gates_fail(inv.sudo()), 'epd',
            )

    # ==================================================================
    # lw_cc_surcharge_optout blocks every
    # credit-card-surcharge gate, independently of the pre-existing
    # lw_cc_service_charge_optout (Charge Terms Interest) flag.
    # ==================================================================

    def _create_portal_user(self, login):
        """A dedicated portal login + partner, isolated from self.partner
        (which has no login and is shared by the tests above)."""
        user = self.env['res.users'].create({
            'name': 'LwCc CC Optout Portal %s' % login,
            'login': login,
            'password': 'LwCcOptout1!',
            'group_ids': [Command.set(
                self.env.ref('base.group_portal').ids,
            )],
        })
        return user, user.partner_id

    def _ensure_transaction_fixture(self):
        """Lazily build the provider/journal/card-method fixture needed
        to hit /invoice/transaction/... (not needed by the quote-only
        tests above, so this stays out of setUp entirely -- same
        'test'-state Authorize provider shape as test_uplift_recompute.py:
        flow='direct' only creates the draft transaction, no live
        provider call happens here).
        """
        if getattr(self, '_lw_cc_tx_fixture_ready', False):
            return
        provider_journal = self.env['account.journal'].create({
            'name': 'LwCc BIN Optout Provider Bank',
            'type': 'bank',
            'code': 'LBOK',
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
                'name': 'LwCc BIN Optout Authorize',
                'code': 'authorize',
                'state': 'test',
                'is_published': True,
            })
        provider.write({
            'state': 'test',
            'journal_id': provider_journal.id,
            'authorize_login': 'TEST-LOGIN',
            'authorize_transaction_key': 'TEST-KEY',
            'authorize_signature_key': 'TEST-SIGNATURE',
        })
        provider_line = provider_journal.inbound_payment_method_line_ids\
            .filtered(lambda l: l.payment_provider_id == provider)
        if not provider_line:
            self.env['account.payment.method.line'].create({
                'journal_id': provider_journal.id,
                'name': 'LwCc BIN Optout Authorize In',
                'payment_method_id': manual_in.id,
                'payment_provider_id': provider.id,
            })
        self.provider = provider
        self.card_method = self.env['payment.method'].search([
            ('code', '=', 'card'),
        ], limit=1) or self.env['payment.method'].create({
            'name': 'Test card', 'code': 'card',
        })
        self._lw_cc_tx_fixture_ready = True

    def _invoice_tx(self, invoice, amount):
        """POST /invoice/transaction/<id>, public auth (matches the real
        route: no login required with a valid access_token)."""
        resp = self.url_open('/invoice/transaction/%s' % invoice.id, json={
            'jsonrpc': '2.0', 'method': 'call', 'id': 1,
            'params': {
                'access_token': invoice._portal_ensure_token(),
                'provider_id': self.provider.id,
                'payment_method_id': self.card_method.id,
                'token_id': None,
                'amount': amount,
                'flow': 'direct',
                'tokenization_requested': False,
                'landing_route': '/my/invoices',
            },
        })
        self.assertEqual(resp.status_code, 200, resp.content[:1000])
        data = json.loads(resp.content)
        if data.get('error'):
            self.fail('jsonrpc error: %s' % data['error'])
        return data.get('result')

    def _overdue_tx(self, amount, payment_reference):
        """POST /invoice/transaction/overdue (requires a logged-in
        session: the route itself rejects public users)."""
        resp = self.url_open('/invoice/transaction/overdue', json={
            'jsonrpc': '2.0', 'method': 'call', 'id': 1,
            'params': {
                'payment_reference': payment_reference,
                'provider_id': self.provider.id,
                'payment_method_id': self.card_method.id,
                'token_id': None,
                'amount': amount,
                'flow': 'direct',
                'tokenization_requested': False,
                'landing_route': '/my/invoices',
            },
        })
        self.assertEqual(resp.status_code, 200, resp.content[:1000])
        data = json.loads(resp.content)
        if data.get('error'):
            self.fail('jsonrpc error: %s' % data['error'])
        return data.get('result')

    def _tx_by_reference(self, processing_values):
        tx = self.env['payment.transaction'].sudo().search([
            ('reference', '=', processing_values['reference']),
        ], limit=1)
        self.assertTrue(tx, "the transaction must have been created")
        return tx

    def test_optout_blocks_single_invoice_quote(self):
        """A partner opted out of the CC surcharge answers 'gates' on the
        single-invoice quote, even though every other gate (term, pct,
        flag) still passes."""
        inv = self._create_invoice(100.0, self.term_net30)
        self.partner.sudo().lw_cc_surcharge_optout = True
        result = self._quote(inv.id, inv._portal_ensure_token(), '555555')
        self.assertEqual(result.get('applies'), False)
        self.assertEqual(result.get('reason'), 'gates')

    def test_optout_blocks_overdue_quote(self):
        """The same opt-out blocks the overdue-batch quote too."""
        portal_user, partner = self._create_portal_user('lw_cc_optout_q_od')
        partner.sudo().lw_cc_surcharge_optout = True
        inv = self._create_invoice(
            100.0, self.term_net30, partner=partner,
        )
        inv.invoice_date_due = self.today - timedelta(days=5)
        self.authenticate(portal_user.login, 'LwCcOptout1!')

        result = self._quote(0, '', '555555', overdue=True)
        self.assertEqual(result.get('applies'), False)
        self.assertEqual(result.get('reason'), 'gates')

    def test_optout_blocks_uplift_kwargs_single_invoice(self):
        """The opt-out also blocks the transaction-amount uplift itself
        (not just the quote), and does so even when a LEGITIMATE session
        verdict already exists -- proving the block comes from the
        uplift-kwargs gate itself, not merely from nothing ever having
        been quoted (the verdict-consumption step at :430-432 would bail
        just as silently on an unquoted invoice, so a test that never
        quotes first cannot tell the two apart).

        Includes a positive control (opt-out OFF, same quote-then-pay
        shape, on a sibling invoice) so removing the opt-out gate would
        make this test's own assertions go red, not pass vacuously.
        """
        self._ensure_transaction_fixture()

        # Positive control: opt-out OFF throughout. Establishes the
        # quote-then-pay flow genuinely uplifts when nothing blocks it.
        inv_control = self._create_invoice(100.0, self.term_net30)
        control_quote = self._quote(
            inv_control.id, inv_control._portal_ensure_token(), '555555',
            amount=100.0,
        )
        self.assertEqual(control_quote.get('applies'), True, msg=str(control_quote))
        with mute_logger('odoo.addons.payment.models.payment_transaction'):
            control_result = self._invoice_tx(inv_control, 100.0)
        control_tx = self._tx_by_reference(control_result)
        self.assertAlmostEqual(control_tx.amount, 103.0, places=2)
        self.assertAlmostEqual(
            control_tx.lw_cc_surcharge_fee_amount, 3.0, places=2,
        )

        # Real assertion: quote FIRST while opt-out is still OFF (a real
        # session verdict gets seeded), THEN flip opt-out ON, THEN pay.
        inv = self._create_invoice(100.0, self.term_net30)
        quote = self._quote(
            inv.id, inv._portal_ensure_token(), '555555', amount=100.0,
        )
        self.assertEqual(quote.get('applies'), True, msg=str(quote))
        self.partner.sudo().lw_cc_surcharge_optout = True

        with mute_logger('odoo.addons.payment.models.payment_transaction'):
            result = self._invoice_tx(inv, 100.0)
        tx = self._tx_by_reference(result)

        self.assertAlmostEqual(tx.amount, 100.0, places=2)
        self.assertFalse(tx.lw_cc_surcharge_fee_amount)

    def test_optout_blocks_uplift_kwargs_overdue_batch(self):
        """Batch fail-safe: the opted-out partner's WHOLE overdue batch
        (both invoices) is blocked from the uplift, not just one line of
        it -- the _lw_cc_uplift_transaction_kwargs gate runs an ``any()``
        check across every invoice in the batch. (The overdue route's own
        domain scopes every invoice in a batch to one partner_id, so a
        genuinely mixed-partner batch cannot be constructed through any
        live calling convention today; this proves the whole single-
        partner batch is blocked as one unit, which is what that any()
        guards in practice.)

        As above: a LEGITIMATE overdue-quote verdict is seeded before
        opt-out is flipped ON, so the block cannot be mistaken for "no
        quote was ever made" -- and a positive control (a separate
        opted-in partner/batch) proves the same flow uplifts when
        nothing blocks it.
        """
        self._ensure_transaction_fixture()

        # Positive control: separate partner/batch, opt-out OFF
        # throughout. A second, independent batch is used (rather than
        # re-quoting the same invoices) because once "paid" (even via a
        # draft transaction) the invoices stay payment_state='not_paid'
        # and would otherwise re-enter a second overdue batch's domain.
        control_user, control_partner = self._create_portal_user(
            'lw_cc_optout_q_batch_ctrl',
        )
        c_inv1 = self._create_invoice(100.0, self.term_net30, partner=control_partner)
        c_inv2 = self._create_invoice(50.0, self.term_net30, partner=control_partner)
        c_inv1.invoice_date_due = self.today - timedelta(days=5)
        c_inv2.invoice_date_due = self.today - timedelta(days=5)
        self.authenticate(control_user.login, 'LwCcOptout1!')
        control_quote = self._quote(0, '', '555555', overdue=True)
        self.assertEqual(control_quote.get('applies'), True, msg=str(control_quote))
        with mute_logger('odoo.addons.payment.models.payment_transaction'):
            control_result = self._overdue_tx(150.0, 'lw_cc-OPTOUT-OD-CTRL')
        control_tx = self._tx_by_reference(control_result)
        self.assertAlmostEqual(control_tx.amount, 154.5, places=2)
        self.assertAlmostEqual(
            control_tx.lw_cc_surcharge_fee_amount, 4.5, places=2,
        )

        # Real assertion: quote FIRST (opt-out still OFF), THEN flip
        # opt-out ON, THEN pay the batch.
        portal_user, partner = self._create_portal_user('lw_cc_optout_q_batch')
        inv1 = self._create_invoice(100.0, self.term_net30, partner=partner)
        inv2 = self._create_invoice(50.0, self.term_net30, partner=partner)
        inv1.invoice_date_due = self.today - timedelta(days=5)
        inv2.invoice_date_due = self.today - timedelta(days=5)
        self.authenticate(portal_user.login, 'LwCcOptout1!')
        quote = self._quote(0, '', '555555', overdue=True)
        self.assertEqual(quote.get('applies'), True, msg=str(quote))
        partner.sudo().lw_cc_surcharge_optout = True

        with mute_logger('odoo.addons.payment.models.payment_transaction'):
            result = self._overdue_tx(150.0, 'lw_cc-OPTOUT-OD')
        tx = self._tx_by_reference(result)

        self.assertAlmostEqual(tx.amount, 150.0, places=2)
        self.assertFalse(tx.lw_cc_surcharge_fee_amount)
        self.assertEqual(set(tx.invoice_ids.ids), {inv1.id, inv2.id})
