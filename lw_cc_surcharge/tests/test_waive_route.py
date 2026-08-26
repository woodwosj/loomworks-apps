# -*- coding: utf-8 -*-
"""Impersonated portal waive tests.

Drives the real invoice transaction routes with jsonrpc POSTs, exactly
like test_uplift_recompute.py, plus one HTML page fetch for the
server-side template omission check:

  - Impersonated session + lw_cc_waive: the fee is waived (amount
    stays at base), and an audit chatter note names the staff uid.
  - SECURITY: lw_cc_waive posted with NO impersonate_from_uid in the
    session is ignored -- the fee still applies -- rather than honored.
  - The overdue batch route honors the same waive.
  - The waive checkbox node is server-side ABSENT from a real customer's
    page HTML (not merely CSS-hidden), and present for an impersonated
    session.

No payment_demo: the Authorize provider runs in state 'test'; flow
'direct' only creates the draft transaction (the charge happens later at
the provider controller), so none of this depends on a live/enabled
payment provider -- the exact posture test_uplift_recompute.py already
relies on for the same routes.
"""
import json
from datetime import timedelta

from odoo import Command, fields
from odoo.tests import HttpCase, tagged
from odoo.tools import mute_logger


@tagged('post_install', '-at_install', 'lw_cc_surcharge')
class TestPortalUpliftWaiveRoute(HttpCase):

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
                'code': '999902',
                'account_type': 'income',
            })

        # 19.0.5.0.0: lw_cc_surcharge_dry_run now gates the CARD FEE
        # as well as the monthly interest. The waive route only means
        # anything when there is a live fee to waive, so this fixture
        # must ask for live mode explicitly. Written here, not in the
        # flag write above, because
        # _check_live_mode_requires_income_account refuses an enabled
        # company in live mode with no Service Charge Income Account.
        self.company.sudo().write({
            'lw_cc_surcharge_income_account_id': self.income_account.id,
            'lw_cc_surcharge_dry_run': False,
        })

        self.term_net30 = self.env['account.payment.term'].create({
            'name': 'LwCc BIN Waive Net 30',
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

        # Portal user paying their own invoices (the real customer).
        self.portal_login = 'lwcc_waive_portal'
        self.portal_user = self.env['res.users'].create({
            'name': 'LwCc BIN Waive Customer',
            'login': self.portal_login,
            'password': 'LumbinWaive1!',
            'group_ids': [Command.set(
                self.env.ref('base.group_portal').ids,
            )],
        })
        self.partner = self.portal_user.partner_id

        # An internal user standing in for the rep who would Switch Login
        # into the customer's portal session. This test seeds the session
        # key directly (session_extra) rather than driving OCA's
        # impersonate_login() -- that mechanism (group + internal-user
        # guard) is that module's own tested territory; this suite tests
        # ONLY how lw_cc_surcharge reacts to the session
        # key impersonate_login() sets.
        self.staff_user = self.env['res.users'].create({
            'name': 'LwCc BIN Waive Staff',
            'login': 'lwcc_waive_staff',
            'group_ids': [Command.set(
                self.env.ref('base.group_user').ids,
            )],
        })

        # Payment plwccg (same shape as test_uplift_recompute.py): a
        # 'test'-state Authorize provider + card method, so /invoice/
        # transaction/<id> can create a draft transaction ('direct' flow
        # only creates the draft; the charge itself happens later at the
        # provider controller, which this suite never reaches).
        self.provider_journal = self.env['account.journal'].create({
            'name': 'LwCc BIN Waive Provider Bank',
            'type': 'bank',
            'code': 'LBWK',
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
                'name': 'LwCc BIN Waive Authorize',
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
        provider_line = self.provider_journal.inbound_payment_method_line_ids\
            .filtered(lambda l: l.payment_provider_id == provider)
        if not provider_line:
            self.env['account.payment.method.line'].create({
                'journal_id': self.provider_journal.id,
                'name': 'LwCc BIN Waive Authorize In',
                'payment_method_id': manual_in.id,
                'payment_provider_id': provider.id,
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

    def _create_invoice(self, amount, due_offset_days=None):
        move_vals = {
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_date': self.today - timedelta(days=1),
            'invoice_payment_term_id': self.term_net30.id,
            'invoice_line_ids': [Command.create({
                'name': 'Test Product',
                'quantity': 1,
                'price_unit': amount,
                'account_id': self.income_account.id,
            })],
        }
        move = self.env['account.move'].create(move_vals)
        move.action_post()
        if due_offset_days is not None:
            # Stamp AFTER posting: action_post recomputes invoice_date_due
            # from the payment term, which would undo a pre-post write.
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

    def _authenticate_portal(self, impersonate_from_uid=None):
        """Login as the customer; optionally seed the impersonation key.

        ``session_extra`` is merged into the session dict at save time
        (odoo/tests/common.py HttpCase.authenticate) -- the same session
        key OCA impersonate_login() sets on a real Switch Login
        (impersonate_login/models/res_users.py:41), just seeded directly
        instead of driving that module's own flow.
        """
        session_extra = None
        if impersonate_from_uid:
            session_extra = {'impersonate_from_uid': impersonate_from_uid}
        self.authenticate(
            self.portal_login, 'LumbinWaive1!', session_extra=session_extra,
        )

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

    def _invoice_tx(self, invoice, amount, **extra):
        params = {
            'access_token': invoice._portal_ensure_token(),
            'provider_id': self.provider.id,
            'payment_method_id': self.card_method.id,
            'token_id': None,
            'amount': amount,
            'flow': 'direct',
            'tokenization_requested': False,
            'landing_route': '/my/invoices',
        }
        params.update(extra)
        return self._jsonrpc('/invoice/transaction/%s' % invoice.id, params)

    def _overdue_tx(self, amount, payment_reference, **extra):
        params = {
            'payment_reference': payment_reference,
            'provider_id': self.provider.id,
            'payment_method_id': self.card_method.id,
            'token_id': None,
            'amount': amount,
            'flow': 'direct',
            'tokenization_requested': False,
            'landing_route': '/my/invoices',
        }
        params.update(extra)
        return self._jsonrpc('/invoice/transaction/overdue', params)

    def _tx(self, processing_values):
        tx = self.env['payment.transaction'].sudo().search([
            ('reference', '=', processing_values['reference']),
        ], limit=1)
        self.assertTrue(tx, "the transaction must have been created")
        return tx

    # ------------------------------------------------------------------
    # Waive honored when genuinely impersonated
    # ------------------------------------------------------------------

    def test_waive_honored_when_impersonated(self):
        """lw_cc_waive + a real impersonate_from_uid: no uplift, and
        the audit chatter names the staff user."""
        inv = self._create_invoice(100.0)
        self._authenticate_portal(impersonate_from_uid=self.staff_user.id)

        quote = self._quote(inv, amount=100.0)
        self.assertEqual(quote.get('applies'), True, msg=str(quote))

        with mute_logger('odoo.addons.payment.models.payment_transaction'):
            result = self._invoice_tx(inv, 100.0, lw_cc_waive=True)
        tx = self._tx(result)

        # Waived: the charged amount stays at the base, no fee at all.
        self.assertAlmostEqual(tx.amount, 100.0, places=2)
        self.assertFalse(tx.lw_cc_surcharge_fee_amount)

        waive_notes = inv.message_ids.filtered(
            lambda m: 'lw_cc_surcharge:fee_waived' in (m.body or '')
        )
        self.assertTrue(
            waive_notes, "the waive must post an audit chatter note",
        )
        self.assertIn(self.staff_user.display_name, waive_notes[0].body)

    # ------------------------------------------------------------------
    # SECURITY: a forged waive with no impersonation session is ignored
    # ------------------------------------------------------------------

    def test_waive_forged_without_impersonation_is_ignored(self):
        """lw_cc_waive posted by a real (non-impersonated) customer
        session must be ignored: the fee still applies, and popping the
        kwarg before the core whitelist means the route does not 400
        (asserted implicitly by _jsonrpc's status_code check below --
        a rejected kwarg would come back as an HTTP 400, not a clean
        jsonrpc result)."""
        inv = self._create_invoice(100.0)
        self._authenticate_portal()  # no impersonate_from_uid at all

        quote = self._quote(inv, amount=100.0)
        self.assertEqual(quote.get('applies'), True, msg=str(quote))

        with mute_logger('odoo.addons.payment.models.payment_transaction'):
            result = self._invoice_tx(inv, 100.0, lw_cc_waive=True)
        tx = self._tx(result)

        # Forged flag ignored: the fee still applies, exactly as if
        # lw_cc_waive had never been sent.
        self.assertAlmostEqual(tx.amount, 103.0, places=2)
        self.assertAlmostEqual(tx.lw_cc_surcharge_fee_amount, 3.0, places=2)

        waive_notes = inv.message_ids.filtered(
            lambda m: 'lw_cc_surcharge:fee_waived' in (m.body or '')
        )
        self.assertFalse(
            waive_notes,
            "a forged waive with no impersonation session must not be "
            "audited as if it were honored",
        )

    # ------------------------------------------------------------------
    # The overdue batch route honors the same waive
    # ------------------------------------------------------------------

    def test_waive_honored_on_overdue_batch(self):
        inv1 = self._create_invoice(100.0, due_offset_days=-5)
        inv2 = self._create_invoice(50.0, due_offset_days=-5)
        self._authenticate_portal(impersonate_from_uid=self.staff_user.id)

        quote = self._jsonrpc('/lw_cc_surcharge/quote', {
            'overdue': True,
            'card_bin': '555555',
        })
        self.assertEqual(quote.get('applies'), True, msg=str(quote))
        self.assertAlmostEqual(quote.get('base'), 150.0, places=2)

        with mute_logger('odoo.addons.payment.models.payment_transaction'):
            result = self._overdue_tx(
                150.0, 'LWCC-WAIVE-OD', lw_cc_waive=True,
            )
        tx = self._tx(result)

        self.assertAlmostEqual(tx.amount, 150.0, places=2)
        self.assertFalse(tx.lw_cc_surcharge_fee_amount)
        self.assertEqual(set(tx.invoice_ids.ids), {inv1.id, inv2.id})

    # ------------------------------------------------------------------
    # Template: server-side omission for a real customer, WHILE the
    # payment widget is confirmed present (HARD once that's true); and
    # presence when impersonated (same guard, same reasoning) -- see
    # each docstring for why both need the positive control.
    # ------------------------------------------------------------------

    def test_waive_node_absent_for_real_customer(self):
        """Absent from a genuine customer's page HTML, while the
        surrounding payment widget is confirmed to have actually
        rendered.

        Correction from an earlier version of this test: asserting only
        that the checkbox id is absent is true but NOT meaningful on its
        own -- on an environment with no available payment provider
        (e.g. a neutralized dev/staging snapshot), the whole payment.form
        widget never renders at all, so the id is trivially absent
        regardless of whether the server-side gate works. That version
        could never fail, which means it proved nothing on exactly the
        environment this project has been burned by before.

        The fix: assert the widget DID render first (same
        'o_payment_form_options' black-box marker as the sibling test,
        payment/views/payment_form_templates.xml), skipTest with a named
        reason if it did not, and only THEN assert the waive node is
        absent. That turns "trivially absent" into "absent while the
        form was present" -- the claim actually being made -- and it is
        the half that must go red if the server-side gate is ever
        broken, on any environment where the check can run at all.
        """
        inv = self._create_invoice(100.0)
        token = inv._portal_ensure_token()

        self._authenticate_portal()  # real customer, no impersonation
        resp = self.url_open('/my/invoices/%s?access_token=%s' % (inv.id, token))
        self.assertEqual(resp.status_code, 200, resp.content[:1000])
        if 'o_payment_form_options' not in resp.text:
            self.skipTest(
                "payment widget (o_payment_form_options) did not render "
                "on this page -- no payment provider available in this "
                "environment (e.g. a neutralized snapshot disables all "
                "providers). This is a payment-provider dependency, not "
                "a regression in the waive template gate; see "
                "test_waive_node_present_when_impersonated for the "
                "sibling half of this check."
            )
        self.assertNotIn(
            'lw_cc_surcharge_waive_checkbox', resp.text,
            "a real customer's page must never contain the waive node, "
            "not even hidden, while the payment widget is present",
        )

    def test_waive_node_present_when_impersonated(self):
        """GUARDED: only asserts presence once the payment widget itself
        is confirmed to have rendered.

        A neutralized dev/staging snapshot (or any environment with no
        available payment provider) disables the whole payment.form
        widget -- ours included -- and no amount of correct code in this
        module makes the checkbox appear on a page that never renders
        the surrounding form at all. Detected via a payment-module DOM
        marker this test does not own ('o_payment_form_options', the
        anchor our own xpath patches onto -- payment/views/
        payment_form_templates.xml), not by inspecting this module's
        own internals. If that marker is missing, the widget did not
        render for a reason outside this module's control: skip with a
        named reason rather than fail, so a provider-availability gap
        elsewhere in the environment never reads as a regression here.
        """
        inv = self._create_invoice(100.0)
        token = inv._portal_ensure_token()

        self._authenticate_portal(impersonate_from_uid=self.staff_user.id)
        resp = self.url_open('/my/invoices/%s?access_token=%s' % (inv.id, token))
        self.assertEqual(resp.status_code, 200, resp.content[:1000])
        if 'o_payment_form_options' not in resp.text:
            self.skipTest(
                "payment widget (o_payment_form_options) did not render "
                "on this page -- no payment provider available in this "
                "environment (e.g. a neutralized snapshot disables all "
                "providers). This is a payment-provider dependency, not "
                "a regression in the waive template gate; see "
                "test_waive_node_absent_for_real_customer for the "
                "sibling half of this check."
            )
        self.assertIn(
            'lw_cc_surcharge_waive_checkbox', resp.text,
            "an impersonated session must render the waive node once "
            "the payment widget itself has rendered",
        )
