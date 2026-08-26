# -*- coding: utf-8 -*-
"""Backend Pay-wizard CC surcharge checkbox tests.

Deliberately provider-independent: every test either reads the wizard's
computed fields directly or calls ``_create_payment_vals_from_wizard``
directly, never ``action_create_payments()`` / ``_send_payment_request``.
A neutralized dev/staging snapshot disables ALL payment providers (no
payment form renders there), so any test depending on a live/enabled
provider is unrunnable off this pod class -- the gate logic under test
here (token verdict, opt-out, EPD, the tamper re-check, the inert
default, the legacy-engine suppression) is fully exercisable without
one. Mirrors the direct-method-call precedent already in this package:
test_quote_route.py's EPD test calls the controller's gate helper
directly rather than driving a real payment.

  - precheck follows the selected token's BIN verdict (CREDIT/empty/DEBIT)
  - staff unchecking the box waives the fee
  - TAMPER: a forged lw_cc_apply_cc_surcharge=True cannot survive the
    server-side re-check when the gates fail
  - a partner's CC surcharge opt-out blocks the wizard path
  - an early-payment-discount invoice is excluded
  - lw_cc_surcharge_backend_wizard ships ON by default (
    the staff auto-tick goes live with the surcharge
    itself. With the flag switched off explicitly, the wizard stays
    INERT (no fee, field unavailable) even with a CREDIT token selected
  - the _assess_cc_surcharge suppression added alongside this flag: a
    WAIVED backend payment must not pick up a surprise legacy fee when
    portal_uplift happens to be off
  - (adverse review): the narrowed suppression gate does NOT
    over-suppress a direct account.payment form token payment that
    never touched the wizard (the documented "known gap" path)
  - Adverse review: a stranded fallback CCS invoice gets
    reconciled against the SAME payment that already collected its fee
    -- _lw_cc_reconcile_stranded_cc_fallback, called directly (a
    payment.move_id needs to exist and be posted for this to mean
    anything, but no charge/provider machinery is needed to produce
    that state)
  - the currency-mismatch gate stays pinned (test_foreign_currency_...)
  - 19.0.5.0.0: dry-run gates the CARD FEE too (wizard gate + portal
    quote gate), each paired with a live-mode test so "no fee" cannot
    pass for the wrong reason

Fixture mirrors test_settlement.py (company flags, income account,
product, Net 30 term, partner, provider + journal + card method).
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.lw_cc_surcharge.controllers.payment_portal \
    import LwCcSurchargePaymentPortal


@tagged('post_install', '-at_install', 'lw_cc_surcharge')
class TestBackendPayWizardSurcharge(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.company = cls.env.company
        # Backend wizard flag starts False (shipped default) -- only the
        # dedicated inert-default test relies on that baseline; every
        # other test arms it explicitly. Portal uplift stays False
        # throughout: these tests are about the BACKEND path only.
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
                'name': 'Test CC Surcharge Income',
                'code': '999902',
                'account_type': 'income',
            })
        cls.company.lw_cc_surcharge_cc_income_account_id = cls.income_account.id

        # 19.0.5.0.0: lw_cc_surcharge_dry_run now gates the CARD FEE
        # as well as the monthly interest, and the wizard gate
        # (_lw_cc_surcharge_gates_pass) is one of the five decision
        # sites that reads it. Every fee-expecting test here therefore
        # needs live mode explicitly. The dry-run side is pinned by
        # test_dry_run_blocks_wizard_gate below. Written here, not in
        # the flag write above, because
        # _check_live_mode_requires_income_account refuses an enabled
        # company in live mode with no Service Charge Income Account.
        cls.company.write({
            'lw_cc_surcharge_income_account_id': cls.income_account.id,
            'lw_cc_surcharge_dry_run': False,
        })

        cls.product = cls.env.ref(
            'lw_cc_surcharge.product_service_charge',
            raise_if_not_found=False,
        )
        if cls.product:
            cls.company.lw_cc_surcharge_product_id = cls.product.id

        cls.term_net30 = cls.env['account.payment.term'].create({
            'name': 'LwCc BIN Wizard Net 30',
            'line_ids': [Command.create({
                'value': 'percent',
                'value_amount': 100.0,
                'nb_days': 30,
            })],
        })
        cls.company.lw_cc_surcharge_applicable_term_ids = cls.term_net30

        cls.today = fields.Date.context_today(cls.env.user)
        cls.partner = cls.env['res.partner'].create({
            'name': 'LwCc BIN Wizard Customer',
            'company_id': cls.company.id,
        })

        # Payment plwccg: a bank journal so the wizard's own defaults
        # (journal_id, payment_method_line_id, available_journal_ids)
        # resolve without error, plus an Authorize provider + card
        # method for the token/transaction fixtures. Same pattern as
        # test_settlement.py's setUpClass.
        cls.provider_journal = cls.env['account.journal'].create({
            'name': 'LwCc BIN Wizard Provider Bank',
            'type': 'bank',
            'code': 'LBWB',
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
                'name': 'LwCc BIN Wizard Authorize',
                'code': 'authorize',
                'state': 'test',
                'is_published': True,
            })
        provider.write({
            'state': 'test',
            'journal_id': cls.provider_journal.id,
            'authorize_login': 'TEST-LOGIN',
            'authorize_transaction_key': 'TEST-KEY',
            'authorize_signature_key': 'TEST-SIGNATURE',
        })
        cls.provider = provider
        provider_line = cls.provider_journal.inbound_payment_method_line_ids\
            .filtered(lambda l: l.payment_provider_id == provider)
        if not provider_line:
            cls.env['account.payment.method.line'].create({
                'journal_id': cls.provider_journal.id,
                'name': 'LwCc BIN Wizard Authorize In',
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

    def _create_invoice(
        self, amount, term=None, partner=None, currency=None, journal=None,
    ):
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
        if currency:
            move_vals['currency_id'] = currency.id
        if journal:
            move_vals['journal_id'] = journal.id
        move = self.env['account.move'].create(move_vals)
        move.action_post()
        return move

    def _build_backend_payment_and_charge(self, invoice, fee_amount, token):
        """Build an account.payment + payment.transaction via the REAL
        chain (account.payment.lw_cc_surcharge_fee_amount ->
        _prepare_payment_transaction_vals -> payment.transaction), then
        simulate a successful synchronous charge by force-setting
        tx.state='done' -- the same convention _create_transaction
        already uses elsewhere in this file / test_settlement.py.

        No _send_payment_request / provider I/O anywhere: this calls
        ``_create_payment_transaction()`` directly instead of
        ``payment.action_post()``, so core's own token-charge branch
        (the one that would call ``_send_payment_request``) never runs.
        Returns ``(payment, tx)``.
        """
        payment = self.env['account.payment'].create({
            'payment_type': 'inbound',
            'partner_type': 'customer',
            'partner_id': invoice.partner_id.id,
            'amount': invoice.amount_residual + fee_amount,
            'journal_id': self.provider_journal.id,
            'payment_token_id': token.id,
            'lw_cc_surcharge_fee_amount': fee_amount,
            'lw_cc_surcharge_wizard_payment': True,
        })
        # active_model/active_ids context matches what the wizard's own
        # env carries through to this call in the real flow (the
        # wizard is opened with active_model='account.move',
        # active_ids=[invoice.id], and that context survives all the
        # way to _prepare_payment_transaction_vals via self._context).
        # Without it, core resolves invoice_ids to [] and there is
        # nothing for the fee to apply to.
        transactions = payment.with_context(
            active_model='account.move', active_ids=invoice.ids,
        )._create_payment_transaction()
        tx = transactions
        tx.write({'state': 'done'})
        return payment, tx

    def _foreign_currency(self):
        """A res.currency guaranteed different from the company currency,
        activated (most non-company currencies ship inactive) with an
        explicit rate on file so posting never hits a missing-rate
        fallback ambiguity.

        Deterministic (adverse review, live-deployment run against a
        production-clone database): does NOT depend on any specific
        base.EUR/base.GBP/base.USD xmlid resolving to something other
        than the company's own currency -- that candidate-list
        approach silently broke on the live pod (the picker returned
        the company's OWN currency; caught only because a downstream
        assertion happened to be hard-wired to fail loudly rather than
        let the test quietly exercise nothing). Instead: read
        company.currency_id first, then search ANY currency that is
        not it, and if this database has none at all, create one
        outright rather than depending on what happens to be seeded.
        """
        company_currency = self.company.currency_id
        currency = self.env['res.currency'].search([
            ('id', '!=', company_currency.id),
            ('active', '=', True),
        ], limit=1)
        if not currency:
            # Widen to inactive currencies (still guaranteed != company's).
            currency = self.env['res.currency'].search([
                ('id', '!=', company_currency.id),
            ], limit=1)
        if not currency:
            # Absolute fallback: no dependency on this database having
            # seeded anything beyond the company's own currency.
            currency = self.env['res.currency'].create({
                'name': 'LBX',
                'symbol': 'L$',
                'rounding': 0.01,
                'decimal_places': 2,
            })
        self.assertNotEqual(
            currency.id, company_currency.id,
            "fixture sanity: _foreign_currency() must return a "
            "currency different from the company's -- if this fails, "
            "the HELPER is broken, not the code under test",
        )
        if not currency.active:
            currency.active = True
        self.env['res.currency.rate'].create({
            'currency_id': currency.id,
            'name': self.today,
            'rate': 1.25,
            'company_id': self.company.id,
        })
        return currency

    def _create_token(self, name, verdict=None, partner=None):
        vals = {
            'provider_id': self.provider.id,
            'partner_id': (partner or self.partner).id,
            'payment_method_id': self.card_method.id,
            'provider_ref': name,
        }
        if verdict:
            vals['lw_cc_surcharge_bin_check'] = verdict
        return self.env['payment.token'].create(vals)

    def _make_wizard(self, invoice, token=None):
        """Open the Pay wizard for one invoice, optionally pre-selecting a
        saved token (mirrors a staff user opening Register Payment and
        picking a card on file)."""
        wizard = self.env['account.payment.register'].with_context(
            active_model='account.move', active_ids=invoice.ids,
        ).create({})
        if token:
            wizard.payment_token_id = token.id
            self.assertEqual(
                wizard.payment_token_id, token,
                "fixture sanity: the explicitly selected token must stick",
            )
        return wizard

    def _create_transaction(self, invoice, amount, reference=None):
        """A done card transaction, for the _assess_cc_surcharge
        suppression test (no wizard/provider I/O involved)."""
        tx = self.env['payment.transaction'].create({
            'provider_id': self.provider.id,
            'payment_method_id': self.card_method.id,
            'reference': reference or ('LWCC-WZ-TX-%s' % self.id()),
            'amount': amount,
            'currency_id': self.company.currency_id.id,
            'partner_id': invoice.partner_id.id,
            'invoice_ids': [Command.set(invoice.ids)],
            'state': 'draft',
        })
        tx.write({'state': 'done'})
        return tx

    def _create_dummy_payment(self, partner, wizard_payment=False):
        """A minimal (unposted) account.payment, just to give a
        transaction a truthy payment_id -- simulates the backend
        wizard's ordering (payment_id set before the transaction is
        assessed) without driving the full wizard/charge machinery.

        :param bool wizard_payment: stamp lw_cc_surcharge_wizard_payment
            (True simulates a payment the Pay wizard actually built --
            e.g. a waived checkbox; False, the default, simulates a
            payment created directly on its own form, never touching
            the wizard -- the narrowed-gate scenario)."""
        return self.env['account.payment'].create({
            'payment_type': 'inbound',
            'partner_type': 'customer',
            'partner_id': partner.id,
            'amount': 1.0,
            'journal_id': self.provider_journal.id,
            'lw_cc_surcharge_wizard_payment': wizard_payment,
        })

    # ------------------------------------------------------------------
    # Precheck follows the token verdict
    # ------------------------------------------------------------------

    def test_precheck_follows_token_verdict(self):
        self.company.lw_cc_surcharge_backend_wizard = True

        credit_token = self._create_token('LWCC-WZ-CREDIT', 'CREDIT')
        inv_credit = self._create_invoice(100.0, self.term_net30)
        wizard_credit = self._make_wizard(inv_credit, credit_token)
        self.assertTrue(wizard_credit.lw_cc_surcharge_available)
        self.assertTrue(wizard_credit.lw_cc_apply_cc_surcharge)

        empty_token = self._create_token('LWCC-WZ-EMPTY')
        inv_empty = self._create_invoice(100.0, self.term_net30)
        wizard_empty = self._make_wizard(inv_empty, empty_token)
        self.assertTrue(wizard_empty.lw_cc_surcharge_available)
        self.assertFalse(
            wizard_empty.lw_cc_apply_cc_surcharge,
            "an empty-verdict token (saved outside the BIN-capture form) "
            "must default UNCHECKED -- fail closed",
        )

        debit_token = self._create_token('LWCC-WZ-DEBIT', 'DEBIT')
        inv_debit = self._create_invoice(100.0, self.term_net30)
        wizard_debit = self._make_wizard(inv_debit, debit_token)
        self.assertFalse(
            wizard_debit.lw_cc_surcharge_available,
            "a DEBIT-verdict token must make the surcharge unavailable",
        )

    # ------------------------------------------------------------------
    # Staff unchecks -> waived -> no fee
    # ------------------------------------------------------------------

    def test_staff_unchecks_waives_fee(self):
        self.company.lw_cc_surcharge_backend_wizard = True
        inv = self._create_invoice(100.0, self.term_net30)
        token = self._create_token('LWCC-WZ-WAIVE', 'CREDIT')
        wizard = self._make_wizard(inv, token)
        self.assertTrue(wizard.lw_cc_apply_cc_surcharge)  # pre-checked

        wizard.lw_cc_apply_cc_surcharge = False  # staff unchecks
        base_amount = wizard.amount
        payment_vals = wizard._create_payment_vals_from_wizard({})

        # 'lw_cc_surcharge_fee_amount' not in payment_vals is free
        # here (core's own _create_payment_vals_from_wizard never puts
        # that key there regardless of any code path) -- the amount
        # assertion below is the one with teeth: it proves no fee was
        # added.
        self.assertAlmostEqual(payment_vals['amount'], base_amount, places=2)

    # ------------------------------------------------------------------
    # TAMPER: forged checkbox cannot survive the server-side re-check
    # ------------------------------------------------------------------

    def test_tamper_forged_checkbox_server_side_re_check_wins(self):
        """Forge lw_cc_apply_cc_surcharge=True while the gates fail
        (flag off): the server-side re-check must win -- no fee applied,
        the payment amount unchanged. "Buttons hide, they don't
        authorize."""
        # Control: switch the flag OFF explicitly (the shipped default
        # is ON), so the gates fail regardless of the (CREDIT) token.
        self.company.lw_cc_surcharge_backend_wizard = False
        inv = self._create_invoice(100.0, self.term_net30)
        token = self._create_token('LWCC-WZ-TAMPER', 'CREDIT')
        wizard = self._make_wizard(inv, token)
        self.assertFalse(self.company.lw_cc_surcharge_backend_wizard)
        self.assertFalse(wizard.lw_cc_surcharge_available)

        # Forge the checkbox directly -- same effect on
        # _create_payment_vals_from_wizard as a tampered RPC payload
        # setting this field on the wizard record.
        wizard.lw_cc_apply_cc_surcharge = True

        base_amount = wizard.amount
        payment_vals = wizard._create_payment_vals_from_wizard({})

        # 'lw_cc_surcharge_fee_amount' not in payment_vals is free
        # here (core's own _create_payment_vals_from_wizard never puts
        # that key there regardless of any code path) -- the amount
        # assertion below is the one with teeth: it proves no fee was
        # added.
        self.assertAlmostEqual(payment_vals['amount'], base_amount, places=2)

    # ------------------------------------------------------------------
    # Partner CC opt-out blocks the wizard path
    # ------------------------------------------------------------------

    def test_partner_optout_blocks_wizard(self):
        self.company.lw_cc_surcharge_backend_wizard = True
        optout_partner = self.env['res.partner'].create({
            'name': 'LwCc BIN Wizard Opt-Out Customer',
            'company_id': self.company.id,
            'lw_cc_surcharge_optout': True,
        })
        inv = self._create_invoice(100.0, self.term_net30, partner=optout_partner)
        token = self._create_token(
            'LWCC-WZ-OPTOUT', 'CREDIT', partner=optout_partner,
        )
        wizard = self._make_wizard(inv, token)
        self.assertFalse(wizard.lw_cc_surcharge_available)

    # ------------------------------------------------------------------
    # Early-payment-discount invoice excluded
    # ------------------------------------------------------------------

    def test_epd_invoice_excluded(self):
        """EPD invoices are excluded, same rationale as the portal quote
        gate (in test_quote_route.py): the base _create_payment gates
        the EPD write-off on an EXACT amount match, which a fee-grown
        amount would silently skip.

        HONESTY NOTE (adverse review): this patches
        ``_get_invoice_next_payment_values`` to force
        ``installment_state == 'epd'`` rather than constructing a
        genuinely EPD-eligible invoice (a real early-payment-discount
        term with the payment date inside the discount window). It
        therefore pins the GATE's own branch check
        (``next_values.get('installment_state') == 'epd'`` ->
        ``return False``) -- it does NOT pin Odoo's own EPD detection
        logic (whether a given term/date combination actually resolves
        to 'epd' in the first place). Matching test_quote_route.py's
        that test, which chose the identical patch for the identical reason:
        "building a real EPD term fixture drags in discount
        configuration that v19 keeps changing" -- a narrow, honest test
        of the branch this module owns, not a broad-looking one that
        would silently stop meaning anything if EPD detection itself
        changed underneath it.
        """
        self.company.lw_cc_surcharge_backend_wizard = True
        inv = self._create_invoice(100.0, self.term_net30)
        token = self._create_token('LWCC-WZ-EPD', 'CREDIT')
        wizard = self._make_wizard(inv, token)
        self.assertTrue(wizard.lw_cc_surcharge_available)  # control

        with patch.object(
            type(self.env['account.move']), '_get_invoice_next_payment_values',
            return_value={'installment_state': 'epd'},
        ):
            self.assertFalse(wizard._lw_cc_surcharge_gates_pass())

    # ------------------------------------------------------------------
    # Flag off: the switch still works, wizard stays INERT
    # ------------------------------------------------------------------

    def test_backend_wizard_off_stays_inert(self):
        """With lw_cc_surcharge_backend_wizard switched OFF
        explicitly, the wizard behaves exactly as before: the surcharge
        field is unavailable and no fee is ever added, even for a
        CREDIT token."""
        self.company.lw_cc_surcharge_backend_wizard = False
        self.assertFalse(self.company.lw_cc_surcharge_backend_wizard)
        inv = self._create_invoice(100.0, self.term_net30)
        token = self._create_token('LWCC-WZ-INERT', 'CREDIT')
        wizard = self._make_wizard(inv, token)

        self.assertFalse(wizard.lw_cc_surcharge_available)
        self.assertFalse(wizard.lw_cc_apply_cc_surcharge)

        base_amount = wizard.amount
        payment_vals = wizard._create_payment_vals_from_wizard({})
        # 'lw_cc_surcharge_fee_amount' not in payment_vals is free
        # here (core's own _create_payment_vals_from_wizard never puts
        # that key there regardless of any code path) -- the amount
        # assertion below is the one with teeth: it proves no fee was
        # added.
        self.assertAlmostEqual(payment_vals['amount'], base_amount, places=2)

    # ------------------------------------------------------------------
    # Ships ON: the default is armed, pre-check fires with no extra step
    # ------------------------------------------------------------------

    def test_backend_wizard_ships_armed_by_default(self):
        """The flag ships ON: a fresh company has
        the flag set with NO explicit arming, and the CREDIT-token
        pre-check fires on that default alone -- there is no second
        go-live step for staff-side surcharging."""
        self.assertTrue(self.company.lw_cc_surcharge_backend_wizard)
        inv = self._create_invoice(100.0, self.term_net30)
        token = self._create_token('LWCC-WZ-SHIPSON', 'CREDIT')
        wizard = self._make_wizard(inv, token)
        self.assertTrue(wizard.lw_cc_surcharge_available)
        self.assertTrue(wizard.lw_cc_apply_cc_surcharge)

    # ------------------------------------------------------------------
    # Suppression subtlety: a waived backend payment gets no surprise fee
    # ------------------------------------------------------------------

    def test_waived_backend_payment_no_surprise_legacy_fee(self):
        """A backend Pay-wizard payment where staff WAIVED the surcharge
        (no fee threaded onto the transaction) must not pick up a
        surprise legacy fee when portal_uplift happens to be off -- the
        suppression _assess_cc_surcharge gained alongside the backend
        wizard flag.

        wizard_payment=True: this dummy payment simulates one the Pay
        wizard actually built (staff opened Register Payment, unchecked
        the box) -- as opposed to test_direct_form_payment_keeps_legacy_coverage
        below, which simulates a payment that never touched the wizard
        at all."""
        dummy_payment = self._create_dummy_payment(
            self.partner, wizard_payment=True,
        )
        self.company.lw_cc_surcharge_portal_uplift = False

        # Control: flag switched OFF explicitly (the shipped default
        # is ON) -- the legacy engine still fires on a payment_id-set
        # transaction. Proves the suppression is really gated by the
        # flag, not just "payment_id is set".
        self.company.lw_cc_surcharge_backend_wizard = False
        inv_off = self._create_invoice(1000.0, self.term_net30)
        tx_flag_off = self._create_transaction(
            inv_off, amount=1000.0,
            reference='LWCC-WAIVE-OFF-%s' % self.id(),
        )
        tx_flag_off.payment_id = dummy_payment.id
        tx_flag_off._assess_cc_surcharge()
        self.assertTrue(
            tx_flag_off.lw_cc_surcharge_invoice_id,
            "control: with the backend flag off, the legacy engine must "
            "still fire on a payment_id-set transaction",
        )

        # Flag ARMED, fee waived (never set): suppressed, no surprise fee.
        self.company.lw_cc_surcharge_backend_wizard = True
        inv_waived = self._create_invoice(1000.0, self.term_net30)
        tx_waived = self._create_transaction(
            inv_waived, amount=1000.0,
            reference='LWCC-WAIVE-ON-%s' % self.id(),
        )
        tx_waived.payment_id = dummy_payment.id
        tx_waived._assess_cc_surcharge()
        self.assertFalse(
            tx_waived.lw_cc_surcharge_invoice_id,
            "armed + waived: the legacy engine must be suppressed",
        )

    # ------------------------------------------------------------------
    # : the suppression gate must NOT over-suppress a direct-form
    # token payment (the documented 'known gap', never touching the
    # wizard) -- adverse review regression.
    # ------------------------------------------------------------------

    def test_direct_form_payment_keeps_legacy_coverage(self):
        """A DIRECT account.payment form token payment (never touching
        the Pay wizard) must still fall through to the legacy engine
        even when the backend wizard flag is armed. Before the fix,
        `self.payment_id` alone gated the suppression -- but payment_id
        is set for ANY transaction born from an account.payment,
        including this one. the design notes documents this path as a "known
        gap" -- fee-less by THIS module's design -- but it was never
        meant to lose the LEGACY engine's coverage too; that would be a
        coverage regression hidden behind a flag documented as purely
        additive."""
        self.company.lw_cc_surcharge_backend_wizard = True
        self.company.lw_cc_surcharge_portal_uplift = False
        # NOT wizard_payment=True: simulates a payment created directly
        # on its own form, never touching account.payment.register.
        direct_payment = self._create_dummy_payment(self.partner)
        self.assertFalse(direct_payment.lw_cc_surcharge_wizard_payment)

        inv = self._create_invoice(1000.0, self.term_net30)
        tx = self._create_transaction(
            inv, amount=1000.0,
            reference='LWCC-DIRECTFORM-%s' % self.id(),
        )
        tx.payment_id = direct_payment.id
        tx._assess_cc_surcharge()

        self.assertTrue(
            tx.lw_cc_surcharge_invoice_id,
            "a direct-form token payment (not wizard-owned) must still "
            "get the legacy surcharge even with the backend wizard "
            "flag armed",
        )

    # ------------------------------------------------------------------
    # A stranded fallback CCS invoice must be settled by the SAME
    # payment that already collected its fee -- adverse review.
    # ------------------------------------------------------------------

    def test_stranded_fallback_invoice_reconciled_against_same_payment(self):
        """When the same-invoice fee line fails and _lw_cc_apply_cc_fee
        falls back to a separate CCS invoice, _lw_cc_reconcile_stranded_
        cc_fallback must settle that fallback invoice against the SAME
        payment that already collected the fee -- not leave the CCS
        invoice open AND unapplied credit sitting on the payment.

        Constructs the PRE-condition state directly (a payment for
        base+fee, reconciled against the original invoice for base
        only -- exactly what core's own _reconcile_payments produces
        when the fee line failed to land on the source invoice) rather
        than driving the full wizard/charge/provider machinery, then
        calls the helper directly.
        """
        inv = self._create_invoice(100.0, self.term_net30)

        # A payment for base+fee (103), as the wizard would build it.
        #
        # payment_method_line_id MUST be set explicitly to the
        # provider's own line (root-caused on the live pod, production-
        # clone database): account.payment's default/computed
        # payment_method_line_id resolves to the journal's generic
        # "Manual Payment" line when nothing hints otherwise, and on
        # this database that line has NO payment_account_id configured
        # -- core's own _generate_journal_entry excludes any payment
        # from `need_move` when outstanding_account_id is falsy
        # (account/models/account_payment.py), so action_post() would
        # silently never create a move at all (payment.state stays
        # 'in_process', payment.move_id stays empty) and there would be
        # nothing to reconcile against -- not a bug in
        # _lw_cc_reconcile_stranded_cc_fallback, a fixture gap in this
        # hand-built payment. The REAL wizard-driven flow never hits
        # this: setting payment_token_id (as
        # _build_backend_payment_and_charge does, and as the wizard
        # itself does for every token payment) drives
        # payment_method_line_id to the provider's own line
        # automatically, which DOES have an outstanding account
        # configured -- confirmed empirically via odoo-bin shell on pod
        # 36731858 and confirmed by the fact that the T-4 full-chain
        # tests (which go through that path) already pass.
        provider_line = self.provider_journal.inbound_payment_method_line_ids.filtered(
            lambda l: l.payment_provider_id == self.provider
        )
        payment = self.env['account.payment'].create({
            'payment_type': 'inbound',
            'partner_type': 'customer',
            'partner_id': self.partner.id,
            'amount': 103.0,
            'journal_id': self.provider_journal.id,
            'payment_method_line_id': provider_line.id,
            'lw_cc_surcharge_wizard_payment': True,
        })
        payment.action_post()
        self.assertEqual(
            payment.state, 'in_process',
            "fixture sanity: a real posted-but-unmatched payment",
        )
        self.assertTrue(
            payment.move_id,
            "fixture sanity: the payment's own journal entry must "
            "actually exist -- if this fails, payment_method_line_id "
            "still resolves to a line with no outstanding account",
        )

        # Reproduce core's own _reconcile_payments outcome directly: it
        # matches the payment's receivable line (103) against ONLY the
        # original invoice's term line (100) -- a PARTIAL match. The
        # SMALLER side (the 100.00 invoice) is the one that ends up
        # FULLY settled; the LARGER side (the 103.00 payment) is the
        # one left with 3.00 of its own residual open. It is the
        # payment's line, not the invoice's, that carries the leftover
        # -- asserting inv.amount_residual == 3.0 here was checking
        # the wrong record's field (caught on the live pod run against
        # a production-clone database).
        receivable_account = inv.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable'
        ).account_id
        payment_receivable = payment.move_id.line_ids.filtered(
            lambda l: l.account_id == receivable_account
        )
        invoice_receivable = inv.line_ids.filtered(
            lambda l: l.account_id == receivable_account
        )
        (payment_receivable + invoice_receivable).reconcile()
        self.assertAlmostEqual(
            inv.amount_residual, 0.0, places=2,
            msg="the 100.00 invoice is the SMALLER side of the match "
                "-- it is fully settled, not the one left open",
        )
        self.assertFalse(
            payment_receivable.reconciled,
            "fixture sanity: the payment's own line must still carry "
            "the unmatched 3.00",
        )
        self.assertAlmostEqual(
            abs(payment_receivable.amount_residual), 3.0, places=2,
            msg="the payment's own line -- the LARGER side of the "
                "match -- is the one carrying the unapplied 3.00 "
                "credit, not the invoice (abs(): the payment's "
                "receivable-account line is the opposite debit/credit "
                "orientation from the invoice's, so amount_residual's "
                "sign differs -- only the magnitude is being pinned "
                "here)",
        )

        # The transaction: fee threaded, same-invoice line FAILED (no
        # fee_line_id), payment_id points at the payment above.
        tx = self.env['payment.transaction'].create({
            'provider_id': self.provider.id,
            'payment_method_id': self.card_method.id,
            'reference': 'LWCC-STRAND-%s' % self.id(),
            'amount': 103.0,
            'currency_id': self.company.currency_id.id,
            'partner_id': self.partner.id,
            'invoice_ids': [Command.set(inv.ids)],
            'state': 'done',
            'lw_cc_surcharge_fee_amount': 3.0,
            'payment_id': payment.id,
        })
        # payment_transaction_id is the REVERSE link
        # _lw_cc_reconcile_stranded_cc_fallback actually reads
        # (`tx = payment.payment_transaction_id`) -- it is a plain,
        # independently-writable Many2one, NOT auto-derived from
        # tx.payment_id (root-caused on the live pod: without this
        # line, the method's own tx = payment.payment_transaction_id
        # resolves to an empty recordset and its very first guard
        # silently `continue`s, doing nothing -- looked exactly like a
        # broken fix from the outside). Core's real
        # _create_payment_transaction() sets both directions together
        # (account_payment/models/account_payment.py:198,
        # `payment.payment_transaction_id = transaction`); this
        # hand-built fixture must set the second one explicitly.
        payment.payment_transaction_id = tx.id

        # The fallback CCS invoice, exactly as
        # _lw_cc_fallback_cc_fee_invoice would have created it.
        fallback_invoice = self.env['account.move'].sudo()\
            ._create_cc_surcharge_invoice(
                partner=self.partner,
                surcharge_amount=3.0,
                company=self.company,
                source_invoices=inv,
                transaction=tx,
            )
        self.assertTrue(fallback_invoice)
        self.assertAlmostEqual(fallback_invoice.amount_residual, 3.0, places=2)

        # The method under test does not depend on wizard record state
        # (only on the to_process list), so it can be called on an
        # empty recordset -- same style as test_quote_route.py's EPD test
        # calling the controller's gate helper without a full request.
        wizard = self.env['account.payment.register']
        wizard._lw_cc_reconcile_stranded_cc_fallback([{'payment': payment}])

        self.assertAlmostEqual(
            fallback_invoice.amount_residual, 0.0, places=2,
            msg="the fallback invoice must be fully settled by the "
                "SAME payment",
        )
        self.assertAlmostEqual(
            inv.amount_residual, 0.0, places=2,
            msg="the ORIGINAL invoice's own residual is untouched by "
                "this step -- it was already closed to 0.00 by core's "
                "own reconcile above; this step matches the fallback "
                "invoice against the PAYMENT's leftover residual, not "
                "against this invoice at all",
        )
        payment_receivable_after = payment.move_id.line_ids.filtered(
            lambda l: l.account_id == receivable_account
        )
        self.assertTrue(
            all(payment_receivable_after.mapped('reconciled')),
            "no unapplied credit may remain open on the payment",
        )

    # ------------------------------------------------------------------
    # T-4: the full settlement chain, end to end. Everything above this
    # point stops at _create_payment_vals_from_wizard or a hand-built
    # transaction; nothing exercised account.payment.
    # lw_cc_surcharge_fee_amount -> _prepare_payment_transaction_vals
    # -> payment.transaction -> _post_process -> _lw_cc_apply_cc_fee ->
    # fee line -> reconciliation as one real sequence. This is exactly
    # the chain the fix addressed broke.
    # ------------------------------------------------------------------

    def test_full_chain_happy_path_settles_via_post_process(self):
        """Exercise the REAL chain end to end: the fee threads through
        _prepare_payment_transaction_vals onto a real payment.transaction,
        _post_process applies it via _lw_cc_apply_cc_fee, the same-invoice
        write SUCCEEDS (no guard tripped), and the resulting single
        payment settles the grown invoice in full."""
        self.company.lw_cc_surcharge_backend_wizard = True
        inv = self._create_invoice(100.0, self.term_net30)
        token = self._create_token('LWCC-CHAIN-HAPPY', 'CREDIT')

        payment, tx = self._build_backend_payment_and_charge(inv, 3.0, token)

        # The leg core builds for us: prove the fee actually threaded
        # onto the transaction via _prepare_payment_transaction_vals.
        self.assertAlmostEqual(tx.lw_cc_surcharge_fee_amount, 3.0, places=2)
        self.assertEqual(tx.payment_id, payment)
        self.assertEqual(tx.invoice_ids, inv)

        tx._post_process()

        # The fee line landed on the SAME invoice; no fallback invoice.
        self.assertTrue(tx.lw_cc_surcharge_fee_line_id)
        fee_line = tx.lw_cc_surcharge_fee_line_id
        self.assertEqual(fee_line.move_id, inv)
        self.assertAlmostEqual(inv.amount_total, 103.0, places=2)
        self.assertFalse(tx._lw_cc_find_fallback_cc_fee_invoice())

        # Post the payment -- payment_transaction_id is already set (by
        # _create_payment_transaction above), so core's action_post()
        # skips the token-charge branch entirely (no
        # _send_payment_request call happens); this just posts the
        # payment's own move -- then reconcile it against the
        # now-grown invoice, exactly as core's own _reconcile_payments
        # would.
        payment.action_post()
        receivable_account = inv.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable'
        ).account_id
        payment_receivable = payment.move_id.line_ids.filtered(
            lambda l: l.account_id == receivable_account
        )
        invoice_receivable = inv.line_ids.filtered(
            lambda l: l.account_id == receivable_account
        )
        (payment_receivable + invoice_receivable).reconcile()

        self.assertAlmostEqual(inv.amount_residual, 0.0, places=2)
        self.assertTrue(all(payment_receivable.mapped('reconciled')))

    def test_full_chain_fallback_path_no_stranded_invoice(self):
        """Same REAL chain, but the same-invoice write FAILS
        (hash-locked journal): _lw_cc_apply_cc_fee falls back to a
        separate CCS invoice, and the wizard's
        _lw_cc_reconcile_stranded_cc_fallback must settle it
        against the SAME payment. This test FAILS against the
        The pre-fix code: without that reconciliation call, the
        fallback invoice stays open at 3.00 AND 3.00 of unapplied
        credit stays open on the payment forever -- the exact bug the
        adverse review found. The assertions right before that call
        pin the pre-condition explicitly so the failure, if the fix
        regresses, reads as "still open" rather than something
        confusing.
        """
        self.company.lw_cc_surcharge_backend_wizard = True
        hash_locked_journal = self.env['account.journal'].create({
            'name': 'LwCc BIN Wizard Hash-Locked Sales',
            'code': 'LBWHL',
            'type': 'sale',
            'company_id': self.company.id,
        })
        inv = self._create_invoice(
            100.0, self.term_net30, journal=hash_locked_journal,
        )
        token = self._create_token('LWCC-CHAIN-FALLBACK', 'CREDIT')

        payment, tx = self._build_backend_payment_and_charge(inv, 3.0, token)

        # Posted BEFORE the hash flag is set (mirrors the proven
        # pattern in lw_cc_surcharge/tests/test_service_charge.py's
        # test_07_hash_locked_journal_skipped_and_counted and
        # test_settlement.py's own hash-locked-journal fallback test).
        hash_locked_journal.restrict_mode_hash_table = True
        try:
            with mute_logger(
                'odoo.addons.lw_cc_surcharge.models.payment_transaction',
            ):
                tx._post_process()
        finally:
            hash_locked_journal.restrict_mode_hash_table = False

        # The same-invoice write failed: no fee line, invoice
        # untouched, a fallback CCS invoice exists instead.
        self.assertFalse(tx.lw_cc_surcharge_fee_line_id)
        self.assertAlmostEqual(inv.amount_total, 100.0, places=2)
        fallback_invoice = tx._lw_cc_find_fallback_cc_fee_invoice()
        self.assertTrue(fallback_invoice)
        self.assertAlmostEqual(fallback_invoice.amount_residual, 3.0, places=2)

        # Post the payment (skips the token-charge branch, same as the
        # happy-path test above) and reproduce core's own
        # _reconcile_payments outcome: it matches the payment's 103
        # against the invoice's 100 -- the SMALLER side (the invoice)
        # ends up FULLY settled, and the LARGER side (the payment)
        # keeps 3.00 of its own residual open. inv.amount_residual is
        # therefore 0.00 here, not 3.00 -- asserting 3.00 on the
        # invoice was checking the wrong record (caught on the live
        # pod run against a production-clone database).
        payment.action_post()
        receivable_account = inv.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable'
        ).account_id
        payment_receivable = payment.move_id.line_ids.filtered(
            lambda l: l.account_id == receivable_account
        )
        invoice_receivable = inv.line_ids.filtered(
            lambda l: l.account_id == receivable_account
        )
        (payment_receivable + invoice_receivable).reconcile()
        self.assertAlmostEqual(
            inv.amount_residual, 0.0, places=2,
            msg="the 100.00 invoice is the SMALLER side of the match "
                "-- it is fully settled",
        )
        self.assertFalse(
            payment_receivable.reconciled,
            "fixture sanity: 3.00 of the payment must still be open "
            "here -- this reproduces core's OWN partial match, not "
            "the fix under test",
        )
        self.assertAlmostEqual(
            abs(payment_receivable.amount_residual), 3.0, places=2,
            msg="the payment's own line -- the LARGER side -- carries "
                "the unapplied 3.00 credit, not the invoice (abs(): "
                "opposite debit/credit orientation from the invoice's "
                "line, only the magnitude is pinned here)",
        )

        # THE FIX under test. Without this call, fallback_invoice and
        # payment_receivable both stay open exactly as asserted above,
        # forever -- this is the stranded-invoice fix.
        wizard = self.env['account.payment.register']
        wizard._lw_cc_reconcile_stranded_cc_fallback([{'payment': payment}])

        self.assertAlmostEqual(
            fallback_invoice.amount_residual, 0.0, places=2,
            msg="the fallback invoice must be fully settled",
        )
        self.assertAlmostEqual(
            inv.amount_residual, 0.0, places=2,
            msg="the ORIGINAL invoice's residual is untouched by this "
                "step (it was already 0.00) -- this step matches the "
                "fallback invoice against the PAYMENT's leftover "
                "residual, not against this invoice",
        )
        payment_receivable_after = payment.move_id.line_ids.filtered(
            lambda l: l.account_id == receivable_account
        )
        self.assertTrue(
            all(payment_receivable_after.mapped('reconciled')),
            "no unapplied credit may remain open on the payment",
        )

    # ------------------------------------------------------------------
    # Currency-mismatch gate: unpinned in the original coverage list,
    # added per the separate currency investigation.
    # ------------------------------------------------------------------

    def test_foreign_currency_invoice_gate_blocks_wizard(self):
        """A wizard whose OWN payment currency differs from the company
        currency must make lw_cc_surcharge_available False, so no
        fee is ever offered. This is the wizard's OWN explicit
        ``self.currency_id != company.currency_id`` gate in
        _lw_cc_surcharge_gates_pass -- the portal path gained the
        mirrored gate in controllers/payment_portal.py, tested
        separately below (test_portal_quote_gate_blocks_foreign_currency).

        PRODUCT FINDING (adverse review, live-deployment run against a
        production-clone database), not a fixture bug: the wizard's
        own ``currency_id`` does NOT simply inherit the invoice being
        paid. Core's own compute
        (account/wizard/account_payment_register.py's
        _compute_currency_id) is ``journal_id.currency_id or
        source_currency_id or company_id.currency_id`` -- the SELECTED
        JOURNAL's own forced currency wins over the invoice's, if the
        journal has one set. On the pod, creating a genuinely
        foreign-currency invoice (confirmed: inv.currency_id really
        was foreign) and letting the wizard auto-pick a journal did
        NOT reliably produce a foreign wizard.currency_id -- it still
        reported the company's currency, because whichever journal the
        wizard auto-selected on this database was not itself
        currency-pinned to that foreign currency.
        This is actually the CORRECT gate target, not a gap: the gate
        exists to protect _lw_cc_compute_portal_fee's rounding, which
        only ever receives wizard.amount/wizard.currency_id -- if a
        foreign invoice is paid through a company-currency journal,
        core has ALREADY converted the amount into company currency by
        the time the wizard sees it, so there is no rounding risk to
        guard against in that case. The dangerous case is specifically
        wizard.currency_id itself differing from company currency
        (e.g. a journal pinned to the SAME foreign currency as the
        invoice) -- which is exactly, and only, what the gate checks.
        So this test forces wizard.currency_id directly rather than
        depending on inheritance through an auto-selected journal.
        """
        self.company.lw_cc_surcharge_backend_wizard = True
        inv = self._create_invoice(100.0, self.term_net30)
        token = self._create_token('LWCC-WZ-FX', 'CREDIT')
        wizard = self._make_wizard(inv, token)

        # Control: with nothing forced, the wizard's own currency is
        # the company's -- confirms the gate isn't just always False.
        self.assertEqual(wizard.currency_id, self.company.currency_id)

        foreign_currency = self._foreign_currency()
        wizard.currency_id = foreign_currency.id
        self.assertNotEqual(wizard.currency_id, self.company.currency_id)
        self.assertFalse(
            wizard.lw_cc_surcharge_available,
            "a wizard whose OWN payment currency differs from the "
            "company currency must NOT make the surcharge available",
        )

    def test_portal_quote_gate_blocks_foreign_currency(self):
        """The portal quote gate (controllers/payment_portal.py's
        _lw_cc_quote_gates_fail) must ALSO block a foreign-currency
        invoice -- the currency investigation's fix applied to the
        portal path, mirroring the wizard's own gate above.

        Called directly on a fresh controller instance, same as
        test_quote_route.py's EPD test: _lw_cc_quote_gates_fail
        does not touch odoo.http.request anywhere in its body (unlike
        _lw_cc_uplift_transaction_kwargs, which does throughout and so
        cannot be driven this way), so no HTTP request context or
        provider is needed to exercise it directly.
        """
        self.company.lw_cc_surcharge_portal_uplift = True
        controller = LwCcSurchargePaymentPortal()

        # Control: a same-currency invoice passes the gate.
        domestic_inv = self._create_invoice(100.0, self.term_net30)
        self.assertIsNone(
            controller._lw_cc_quote_gates_fail(domestic_inv.sudo()),
        )

        foreign_currency = self._foreign_currency()
        foreign_inv = self._create_invoice(
            100.0, self.term_net30, currency=foreign_currency,
        )
        self.assertNotEqual(foreign_inv.currency_id, self.company.currency_id)
        self.assertEqual(
            controller._lw_cc_quote_gates_fail(foreign_inv.sudo()), 'gates',
            "a foreign-currency invoice must fail the portal quote gate",
        )

    # ------------------------------------------------------------------
    # Dry-run gates the card fee (19.0.5.0.0)
    # ------------------------------------------------------------------

    def test_dry_run_blocks_wizard_gate_and_charge(self):
        """Dry-run suppresses the backend Pay-wizard fee even when every
        OTHER gate passes: wizard flag armed, module enabled, positive
        percentage, applicable terms configured, CREDIT token.

        That combination is the post-the configuration work configuration -- the one
        that does not exist on production yet and is the planned next
        step. Until 19.0.5.0.0 ``lw_cc_surcharge_dry_run`` was read
        in exactly ONE place in the codebase (the monthly interest
        runner); the card fee ignored it entirely, so this fixture
        charged the customer's card 3% more while Settings said
        "Dry-Run Mode".

        The gate sits at the DECISION site on purpose. The wizard adds
        the fee to the amount charged to the card, so suppressing it at
        settlement instead would take the money and record nothing.
        Asserting on ``payment_vals['amount']`` is what proves the card
        is not charged: the fee never reaches the payment at all.
        """
        self.company.lw_cc_surcharge_backend_wizard = True
        self.company.lw_cc_surcharge_dry_run = True
        try:
            # Guard the fixture: this test is only meaningful while
            # every other gate genuinely passes.
            self.assertTrue(self.company.lw_cc_surcharge_enabled)
            self.assertGreater(self.company.lw_cc_surcharge_cc_pct, 0.0)
            self.assertTrue(
                self.company.lw_cc_surcharge_applicable_term_ids
            )
            self.assertTrue(self.company._lw_cc_fee_is_dry_run())

            inv = self._create_invoice(100.0, self.term_net30)
            token = self._create_token('LWCC-WZ-DRYRUN', 'CREDIT')
            wizard = self._make_wizard(inv, token)

            self.assertFalse(
                wizard._lw_cc_surcharge_gates_pass(),
                "the wizard gate passed in dry-run mode",
            )
            self.assertFalse(
                wizard.lw_cc_surcharge_available,
                "the checkbox was offered to staff in dry-run mode",
            )

            # Even forged: buttons hide, they don't authorize.
            wizard.lw_cc_apply_cc_surcharge = True
            base_amount = wizard.amount
            payment_vals = wizard._create_payment_vals_from_wizard({})
            self.assertAlmostEqual(
                payment_vals['amount'], base_amount, places=2,
                msg="dry-run still uplifted the amount charged to the card",
            )
        finally:
            self.company.lw_cc_surcharge_dry_run = False

    def test_live_mode_still_charges_the_wizard_fee(self):
        """Companion to the test above: with dry-run OFF and the same
        fixture, the gate passes and the fee IS added to the charge.

        Without this pairing the dry-run test would pass just as
        happily against a guard that broke the wizard outright -- "no
        fee" is the expected result of both a correct dry-run gate and
        a severed code path.
        """
        self.company.lw_cc_surcharge_backend_wizard = True
        self.assertFalse(self.company.lw_cc_surcharge_dry_run)

        inv = self._create_invoice(100.0, self.term_net30)
        token = self._create_token('LWCC-WZ-LIVE-PAIR', 'CREDIT')
        wizard = self._make_wizard(inv, token)

        self.assertTrue(wizard._lw_cc_surcharge_gates_pass())
        base_amount = wizard.amount
        payment_vals = wizard._create_payment_vals_from_wizard({})
        # 3% of 100 = 3.00
        self.assertAlmostEqual(
            payment_vals['amount'], base_amount + 3.0, places=2,
        )

    def test_dry_run_blocks_portal_quote_gate(self):
        """The portal quote gate (controllers/payment_portal.py's
        _lw_cc_quote_gates_fail) also fails closed in dry-run.

        Quoting a fee the checkout will not charge would show the
        customer a total that does not match what is taken from their
        card, so dry-run has to suppress the quote as well as the
        uplift.

        Called directly on a fresh controller instance, same precedent
        as test_portal_quote_gate_blocks_foreign_currency above:
        _lw_cc_quote_gates_fail never touches odoo.http.request, so no
        HTTP context or provider is needed.
        """
        self.company.lw_cc_surcharge_portal_uplift = True
        controller = LwCcSurchargePaymentPortal()
        inv = self._create_invoice(100.0, self.term_net30)

        # Control: live mode quotes the fee (the gate returns None).
        self.assertFalse(self.company.lw_cc_surcharge_dry_run)
        self.assertIsNone(controller._lw_cc_quote_gates_fail(inv.sudo()))

        self.company.lw_cc_surcharge_dry_run = True
        try:
            self.assertEqual(
                controller._lw_cc_quote_gates_fail(inv.sudo()), 'gates',
                "the portal quote gate offered a fee in dry-run mode",
            )
        finally:
            self.company.lw_cc_surcharge_dry_run = False
