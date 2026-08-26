# -*- coding: utf-8 -*-
"""payment.transaction override for CC surcharge post-payment.

When a payment transaction is processed successfully for a Net 30+ customer
paying by card, this module creates a separate surcharge invoice for the
surcharge percentage.

The surcharge is a separate invoice (not a modification of the payment amount),
keeping the accounting clean and the payment flow modification-free. The
surcharge invoice is created immediately after the payment is confirmed.

Debit card exclusion via BIN lookup is integrated here: if the
BIN record says the card is debit, the surcharge is skipped.
"""
import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    lw_cc_surcharge_amount = fields.Monetary(
        string="CC Surcharge Amount",
        currency_field='currency_id',
        readonly=True,
        copy=False,
        help="Computed CC surcharge amount for this transaction.",
    )
    lw_cc_surcharge_invoice_id = fields.Many2one(
        'account.move',
        string="CC Surcharge Invoice",
        readonly=True,
        copy=False,
        help="Surcharge invoice created for this transaction.",
    )
    lw_cc_surcharge_bin_check = fields.Char(
        string="BIN Check Result",
        readonly=True,
        copy=False,
        help="Card type from BIN lookup: 'CREDIT', 'DEBIT', or None.",
    )

    # ------------------------------------------------------------------
    # Process override
    # ------------------------------------------------------------------

    def _process(self, provider_code, payment_data):
        """Override to create CC surcharge invoice after successful payment.

        After the transaction is processed, if the transaction is in 'done'
        state and linked to eligible invoices, create a surcharge invoice.
        """
        result = super()._process(provider_code, payment_data)

        # Only create surcharge for successfully completed transactions.
        if self.state != 'done':
            return result

        # Only for card payments, resolved to the PRIMARY method. The
        # gateway rewrites payment_method_id to the card BRAND (visa/
        # mastercard/...) before this runs, so code is 'visa' here, not
        # 'card' -- primary_payment_method_id resolves brand -> 'card'.
        # 'card' and 'ach_direct_debit' have no primary and must fall back
        # to themselves (mirrors core Odoo's own `pm.primary_payment_method_id
        # or pm` idiom in payment_method.py); without the fallback every
        # existing test here breaks, since they build the transaction
        # directly against the 'card' record.
        primary_method = (
            self.payment_method_id.primary_payment_method_id
            or self.payment_method_id
        )
        if primary_method.code != 'card':
            return result

        # Only for Authorize.Net (our only card processor).
        if self.provider_code != 'authorize':
            return result

        try:
            self._assess_cc_surcharge()
        except Exception:  # noqa: BLE001
            _logger.exception(
                "lw_cc_surcharge: error creating surcharge invoice for "
                "transaction %s; payment already complete, continuing.",
                self.reference,
            )

        return result

    # ------------------------------------------------------------------
    # Surcharge assessment
    # ------------------------------------------------------------------

    def _assess_cc_surcharge(self):
        """Assess and create CC surcharge invoice for this transaction.

        Conditions:
          1. The invoice(s) linked to this transaction have Net 30+ terms.
          2. The company has CC surcharge enabled and a percentage set.
          3. The card is NOT debit (BIN lookup).
        """
        self.ensure_one()

        # Card payments only, resolved to the PRIMARY method (see
        # _process() for the rationale). _process() gates as well, but
        # guard here too so a direct call can never surcharge ACH or
        # other rails.
        primary_method = (
            self.payment_method_id.primary_payment_method_id
            or self.payment_method_id
        )
        if primary_method.code != 'card':
            return

        # Check company config.
        company = self.company_id or self.env.company
        if not company.lw_cc_surcharge_enabled:
            return
        cc_pct = company.lw_cc_surcharge_cc_pct or 0.0
        if cc_pct <= 0.0:
            return

        # Per-customer CC surcharge opt-out. Separate from the
        # Charge Terms Interest opt-out; read off the commercial partner.
        if self.partner_id.commercial_partner_id.lw_cc_surcharge_optout:
            _logger.info(
                "lw_cc_surcharge: skipping surcharge for tx %s "
                "(partner opted out of CC surcharge).",
                self.reference,
            )
            return

        # Find linked invoices.
        invoices = self._get_linked_invoices()
        if not invoices:
            return

        # Check if any invoice has Net 30+ terms. FAIL CLOSED on an
        # empty configuration: with no applicable terms configured the
        # surcharge is simply not configured, so nothing is eligible.
        # The previous `not applicable_terms or ...` read the empty set
        # as "applies to everyone" and surcharged Net 0 customers on a
        # half-configured company. Every other surface already fails
        # closed this way -- the portal quote/charge gates
        # (lw_cc_surcharge/controllers/payment_portal.py
        # :168, :255, :403) and the backend Pay wizard
        # (lw_cc_surcharge/wizards/
        # account_payment_register.py:150) all bail on `not
        # applicable_terms`.
        applicable_terms = company.lw_cc_surcharge_applicable_term_ids
        if not applicable_terms:
            _logger.info(
                "lw_cc_surcharge: skipping surcharge for tx %s "
                "(no applicable payment terms configured).",
                self.reference,
            )
            return
        eligible_invoices = invoices.filtered(
            lambda inv: inv.invoice_payment_term_id in applicable_terms
        )

        if not eligible_invoices:
            return

        # BIN lookup: check if card is debit.
        bin_check = self._check_card_bin()
        self.lw_cc_surcharge_bin_check = bin_check
        if bin_check == 'DEBIT':
            _logger.info(
                "lw_cc_surcharge: skipping surcharge for tx %s "
                "(card is debit).",
                self.reference,
            )
            return

        # Calculate surcharge.
        surcharge_amount = self.amount * (cc_pct / 100.0)

        # Dry-run: every gate above passed, so this transaction WOULD
        # have been surcharged. Log what it would have been and return
        # without creating anything.
        #
        # Placed HERE, after all the gates, deliberately: the log line
        # is only worth reading if it describes a fee that genuinely
        # would have applied. Placed at the top of the method it would
        # fire for every ACH payment and every Net 0 customer too.
        #
        # This path is safe to stop at the point of writing because
        # nothing was charged to the card: the legacy engine's fee is a
        # SEPARATE invoice raised after the payment completed. The
        # checkout-time uplift in lw_cc_surcharge is the
        # opposite case and is gated where the fee is decided instead --
        # see res_company._lw_cc_fee_is_dry_run.
        #
        # The one write that has already happened on this path is
        # lw_cc_surcharge_bin_check above. That is deliberate and is
        # not a fee: it records what the BIN lookup said about the card,
        # it is written on every outcome including outright skips (a
        # debit card sets it and returns), and _post_process copies it
        # onto the token so a later payment need not re-capture the
        # digits. No accounting object is created here.
        if company._lw_cc_fee_is_dry_run():
            _logger.info(
                "lw_cc_surcharge [DRY-RUN]: would charge tx %s a "
                "credit card fee of %.2f (%.2f%% of %.2f) on invoice(s) "
                "%s. No surcharge invoice created; untick Dry-Run Mode "
                "in Settings to charge it.",
                self.reference, surcharge_amount, cc_pct, self.amount,
                ", ".join(eligible_invoices.mapped('name')),
            )
            return

        # Create the surcharge invoice.
        surcharge_inv = self.env['account.move'].sudo()._create_cc_surcharge_invoice(
            partner=self.partner_id.commercial_partner_id,
            surcharge_amount=surcharge_amount,
            company=company,
            source_invoices=eligible_invoices,
            transaction=self,
        )

        if surcharge_inv:
            self.lw_cc_surcharge_amount = surcharge_amount
            self.lw_cc_surcharge_invoice_id = surcharge_inv.id
            _logger.info(
                "lw_cc_surcharge: created surcharge invoice %s for "
                "tx %s, amount %.2f.",
                surcharge_inv.name, self.reference, surcharge_amount,
            )

    # ------------------------------------------------------------------
    # Linked invoices
    # ------------------------------------------------------------------

    def _get_linked_invoices(self):
        """Return the invoices linked to this transaction.

        Payment transactions can be linked to invoices via:
        - ``invoice_ids`` m2m field (core Odoo 19)
        - ``statement_line_id`` for bank reconciliation
        """
        self.ensure_one()
        invoices = self.env['account.move']
        if hasattr(self, 'invoice_ids') and self.invoice_ids:
            invoices |= self.invoice_ids
        return invoices.filtered(lambda i: i.move_type == 'out_invoice' and i.state == 'posted')

    # ------------------------------------------------------------------
    # BIN lookup
    # ------------------------------------------------------------------

    def _check_card_bin(self):
        """Look up the card BIN to determine debit/credit.

        Checks the local ``lw_cc.bin.record`` table for the
        card's first 6 digits. Returns 'CREDIT', 'DEBIT', or None.

        The BIN is extracted from the transaction's payment token
        metadata or provider reference. The BIN is populated
        from a client-side capture of the first 6 digits before
        tokenization.

        :returns: 'CREDIT', 'DEBIT', or None if unknown.
        """
        self.ensure_one()

        # BIN extraction from transaction metadata.
        # The BIN is populated from the payment form's
        # client-side capture of the first 6 digits.
        bin_prefix = self._get_bin_prefix()
        if not bin_prefix or len(bin_prefix) < 6:
            return None

        # Look up in the local BIN table.
        BINRecord = self.env['lw_cc.bin.record']
        record = BINRecord.search([
            ('bin_start', '<=', bin_prefix),
            ('bin_end', '>=', bin_prefix),
        ], limit=1)

        if record:
            return record.card_type
        return None

    def _get_bin_prefix(self):
        """Extract the first 6 digits of the card number.

        The BIN comes from client-side capture before
        tokenization, stored as a context value or custom field.
        Falls back to the masked card number on the transaction
        if available.
        """
        self.ensure_one()

        # Try the context first (populated by payment form JS).
        ctx_bin = self.env.context.get('lw_cc_card_bin', '')
        if ctx_bin and len(ctx_bin) >= 6:
            return ctx_bin[:6]

        # Fall back to provider_reference metadata.
        # The BIN is stored in a custom field.
        if hasattr(self, 'lw_cc_card_bin'):
            return self.lw_cc_card_bin or ''

        return ''
# ===================================================================
# Below: checkout-time uplift + backend wizard settlement.
# ===================================================================
# -*- coding: utf-8 -*-
"""payment.transaction override for checkout-time CC surcharge (portal
uplift) and backend Pay-wizard settlement.

Companion to lw_cc_surcharge's post-payment engine (see that module's
payment.transaction). When the portal uplift is armed, the fee is added
to the transaction amount BEFORE the card is charged (one transaction =
invoice + fee). The backend Pay-wizard checkbox (account_payment.py /
wizards/account_payment_register.py) threads the SAME fee fields onto a
token transaction created from the wizard.

Either origin settles through the single entry point
``_lw_cc_apply_cc_fee``: the fee is added as a line on the SAME source
invoice when possible (``_lw_cc_add_cc_fee_line``, delegated to
account_move.py), falling back to the legacy separate CCS invoice
(``CCS/<reference>``, idempotent) on ANY failure so a fee that was
actually charged to the card is never stranded.

Everything here is INERT until the company flag
``lw_cc_surcharge_portal_uplift`` (portal) or
``lw_cc_surcharge_backend_wizard`` (backend) is enabled. With both
flags off, ``_assess_cc_surcharge`` and ``_create_payment`` delegate to
the base module byte-identically.
"""
import logging

from odoo import api, fields, models
from odoo.fields import Command
from odoo.tools import float_round

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    lw_cc_surcharge_fee_amount = fields.Monetary(
        string="Portal Fee Uplift Amount",
        currency_field='currency_id',
        readonly=True,
        copy=False,
        help=(
            "Credit card fee added to the charged amount by the portal "
            "uplift or the backend Pay-wizard checkbox. Unset (or 0) "
            "means the transaction was not uplifted and the legacy "
            "post-payment engine of lw_cc_surcharge applies "
            "unchanged."
        ),
    )
    lw_cc_surcharge_fee_line_id = fields.Many2one(
        'account.move.line',
        string="CC Fee Line",
        readonly=True,
        copy=False,
        help=(
            "The same-invoice charge line that settled this "
            "transaction's CC fee. Empty either means no fee "
            "was applied, or the fee fell back to a separate CCS "
            "invoice (see _lw_cc_apply_cc_fee). Set exactly once -- the "
            "single idempotency marker for _lw_cc_apply_cc_fee."
        ),
    )

    # ------------------------------------------------------------------
    # Fee computation (shared by the controller's quote and uplift paths)
    # ------------------------------------------------------------------

    @api.model
    def _lw_cc_compute_portal_fee(self, base, company, currency=None):
        """Compute the portal uplift fee on ``base`` for ``company``.

        Rounds with ``currency`` (the currency ``base`` is ACTUALLY
        denominated in) so the quoted fee, the charged uplift and the
        fee invoice line always agree to the cent. ``base`` is never
        converted here -- it is a straight percentage of an already
        invoice-currency-denominated amount -- so using the WRONG
        rounding increment (company currency instead of the invoice's)
        is a real, if narrow, defect for a currency pair whose rounding
        increments differ (e.g. a 0-decimal company currency against a
        2-decimal invoice currency); it is numerically academic only
        when both round to the same increment (adverse-review currency
        investigation).

        :param currency: The currency ``base`` is denominated in.
            Defaults to ``company.currency_id`` for BACKWARD
            COMPATIBILITY with existing callers that have not yet been
            updated to pass the invoice's own currency explicitly
            (controllers/payment_portal.py's quote/uplift call sites --
            see the currency investigation report for why they were not
            updated in this pass).
        :param float base: Server-validated invoice base amount.
        :param res.company company: Company whose ``cc_pct`` applies.
        :return: The fee rounded to ``currency``, or 0.0 when the
                 percentage is unset/zero (no uplift).
        :rtype: float
        """
        cc_pct = company.lw_cc_surcharge_cc_pct or 0.0
        if cc_pct <= 0.0:
            return 0.0
        currency = currency or company.currency_id
        return float_round(
            base * (cc_pct / 100.0),
            precision_rounding=currency.rounding,
        )

    # ------------------------------------------------------------------
    # Legacy engine deferral
    # ------------------------------------------------------------------

    def _assess_cc_surcharge(self):
        """Defer invoice-linked transactions to the portal uplift when armed.

        Ordering matters:
          1. A transaction that already carries an uplift fee never needs a
             second (post-payment) surcharge invoice.
          2. When the uplift flag is armed, invoice-linked transactions are
             owned by this module: the fee was either charged upfront or
             deliberately skipped by the checkout gates.
        Everything else (phone payments, website checkout, ...) keeps the
        legacy post-payment behavior of lw_cc_surcharge.
        """
        self.ensure_one()
        if self.lw_cc_surcharge_fee_amount:
            return
        company = self.company_id or self.env.company
        if self.invoice_ids and company.lw_cc_surcharge_portal_uplift:
            return
        # Backend Pay-wizard transactions are owned by this
        # module once the wizard flag is armed, regardless of whether
        # the portal uplift flag happens to be on. Without this, a
        # payment where staff explicitly WAIVED the checkbox (so
        # lw_cc_surcharge_fee_amount is left unset) falls through to
        # the legacy engine and gets a surprise fee anyway.
        #
        # NARROWED (adverse review): `self.payment_id` alone is
        # NOT a reliable "backend wizard origin" marker -- it is also
        # set for a DIRECT account.payment form token payment (staff
        # opens Accounting > Payments, picks a token, posts directly,
        # never touching the register wizard). the design notes documents that
        # path as a "known gap" -- fee-less by THIS module's design --
        # but it was never meant to lose the LEGACY engine's coverage
        # too. `payment_id.lw_cc_surcharge_wizard_payment` is
        # stamped ONLY by account_payment_register.py's
        # `_create_payment_vals_from_wizard` (unconditionally, even
        # when the fee ends up waived/unavailable), so it distinguishes
        # "wizard-owned" from "direct-form" regardless of whether a fee
        # was actually charged.
        if (
            self.payment_id
            and self.payment_id.lw_cc_surcharge_wizard_payment
            and self.invoice_ids
            and company.lw_cc_surcharge_backend_wizard
        ):
            return
        return super()._assess_cc_surcharge()

    # ------------------------------------------------------------------
    # Settlement: one payment for invoice + fee
    # ------------------------------------------------------------------

    def _create_payment(self, **extra_create_values):
        """Settle the uplift fee together with the invoices in ONE payment.

        Ordering: the fee line is added to the source
        invoice(s) via ``_lw_cc_apply_cc_fee`` BEFORE the payment is
        built, so the invoice's grown residual reconciles naturally
        against a base+fee payment -- no transient invoice link is
        needed any more. This override only runs for transactions core
        itself decided need a NEW payment (``not tx.payment_id``, see
        account_payment's own ``_post_process``), i.e. the portal/
        direct-charge path; the backend wizard already has a
        ``payment_id`` by the time its transaction is created, so it
        never reaches here (its fee is applied from ``_post_process``
        instead -- see below).

        The charge already succeeded by the time this runs (core only
        calls ``_create_payment`` for a 'done' transaction), so even a
        total failure of ``_lw_cc_apply_cc_fee`` (which itself falls
        back to a separate CCS invoice) never strands the payment: this
        method always proceeds to build it.

        If ``_lw_cc_apply_cc_fee`` fell back to a separate CCS invoice
        (rather than a same-invoice line), that invoice is transiently
        linked into ``invoice_ids`` so the base ``_create_payment`` still
        includes and reconciles it against THIS payment -- the customer
        already paid the fee (it is part of ``self.amount``), so leaving
        the fallback invoice unlinked would strand it open/unreconciled.
        The link is undone afterwards so ``invoice_ids`` reflects only
        the invoices the customer actually set out to pay (the
        account.payment record itself keeps both, same as before).
        """
        self.ensure_one()
        fallback_invoice = self.env['account.move']
        if self.lw_cc_surcharge_fee_amount and not self.payment_id:
            result = self._lw_cc_apply_cc_fee()
            if result and result._name == 'account.move':
                fallback_invoice = result

        if not fallback_invoice:
            return super()._create_payment(**extra_create_values)

        self.sudo().write({'invoice_ids': [Command.link(fallback_invoice.id)]})
        try:
            return super()._create_payment(**extra_create_values)
        finally:
            self.sudo().write({'invoice_ids': [Command.unlink(fallback_invoice.id)]})

    # ------------------------------------------------------------------
    # Fee settlement: single entry point for portal + backend wizard
    # ------------------------------------------------------------------

    def _lw_cc_apply_cc_fee(self):
        """Apply this transaction's queued CC fee, once, however it arrived.

        Single entry point for BOTH the portal direct-charge path
        (``_create_payment`` above) and the backend Pay-wizard path
        (``_post_process`` below). Multi-invoice overdue batches put the
        fee on the OLDEST invoice by due date (tie-break on id) so it
        ages with the customer's longest-outstanding balance. ANY
        failure on the same-invoice path (locked period, hash-locked
        journal, no eligible invoice, ...) falls back to the legacy
        behavior: a separate ``CCS/<reference>`` invoice.

        Idempotent regardless of how many times or from which caller
        this is invoked for the same transaction: it checks BOTH the
        same-invoice marker (``lw_cc_surcharge_fee_line_id``) and an
        existing fallback invoice before doing any work, so a
        transaction whose fee already settled one way never gets
        charged, invoiced, or logged again the other way.

        The same-invoice attempt runs inside its own savepoint.
        ``_lw_cc_add_cc_fee_line`` writes the fee line BEFORE it
        re-reconciles the snapshotted counterparts (see
        account_move.py's ``_lw_cc_add_charge_line``); a failure in that
        LATER step (e.g. ``.reconcile()`` raising) is a normal Python
        exception, not one that poisons the cursor, so without a
        savepoint the already-written line would survive the rollback
        that never happens -- leaving both a same-invoice line AND a
        fallback CCS invoice for the SAME fee. The savepoint's rollback
        also clears the ORM cache (v19's flushing savepoint calls
        ``cr.clear()``), so the fallback path below never reads stale
        cache describing writes that no longer exist.

        :returns: the ``account.move.line`` (same-invoice) or
            ``account.move`` (fallback) that settled the fee, or an
            empty ``account.move.line`` recordset if there was nothing
            to apply.
        """
        self.ensure_one()
        if self.lw_cc_surcharge_fee_line_id:
            return self.lw_cc_surcharge_fee_line_id
        fee = self.lw_cc_surcharge_fee_amount
        if not fee:
            return self.env['account.move.line']

        existing_fallback = self._lw_cc_find_fallback_cc_fee_invoice()
        if existing_fallback:
            return existing_fallback

        invoices = self._get_linked_invoices()
        target = None
        if invoices:
            # Oldest by due date; tie-break on id for a stable, ordering-
            # independent choice when two invoices share a due date.
            target = min(
                invoices,
                key=lambda inv: (
                    inv.invoice_date_due or fields.Date.from_string('9999-12-31'),
                    inv.id,
                ),
            )

        if target:
            try:
                with self.env.cr.savepoint():
                    return target.sudo()._lw_cc_add_cc_fee_line(fee, self)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "lw_cc_surcharge: same-invoice CC fee "
                    "line failed for tx %s on invoice %s (%s); rolled "
                    "back, falling back to a separate CCS invoice.",
                    self.reference, target.display_name, exc,
                )

        return self._lw_cc_fallback_cc_fee_invoice(invoices)

    def _lw_cc_find_fallback_cc_fee_invoice(self):
        """Return this transaction's existing fallback CCS invoice, if any."""
        self.ensure_one()
        return self.env['account.move'].sudo().search([
            ('ref', '=', 'CCS/%s' % self.reference),
            ('move_type', '=', 'out_invoice'),
            ('state', '!=', 'cancel'),
        ], limit=1)

    def _lw_cc_fallback_cc_fee_invoice(self, invoices=None):
        """Legacy separate-CCS-invoice fallback, idempotent on ``CCS/<ref>``.

        If the fee invoice cannot be created either, the payment still
        proceeds: the card was already charged, so failing here would
        strand it. The missing fee becomes a repair item (logged as an
        error).
        """
        self.ensure_one()
        fee_invoice = self._lw_cc_find_fallback_cc_fee_invoice()
        if not fee_invoice:
            source_invoices = (
                invoices if invoices is not None else self._get_linked_invoices()
            )
            fee_invoice = self.env['account.move'].sudo()._create_cc_surcharge_invoice(
                partner=self.partner_id.commercial_partner_id,
                surcharge_amount=self.lw_cc_surcharge_fee_amount,
                company=self.company_id,
                source_invoices=source_invoices,
                transaction=self,
            )
        if not fee_invoice:
            _logger.error(
                "lw_cc_surcharge: CC fee for tx %s could "
                "not be applied to any invoice (fee %.2f); this is a "
                "repair item -- the card was already charged.",
                self.reference, self.lw_cc_surcharge_fee_amount,
            )
            return fee_invoice

        # : stamp the SAME authoritative marker on the fallback
        # invoice's own line -- _create_cc_surcharge_invoice lives in
        # the base module and cannot be extended with extra_line_vals
        # without editing lw_cc_surcharge (out of scope here), but
        # its result is a normal recordset this module can write to
        # directly. invoice_line_ids (not line_ids) is exactly the one
        # product line _create_cc_surcharge_invoice creates -- it
        # excludes the auto-generated receivable/tax lines. Unconditional
        # (not just on first creation) so a pre-existing fallback
        # invoice found via _lw_cc_find_fallback_cc_fee_invoice above
        # also ends up marked, not only a freshly-created one.
        fee_invoice.invoice_line_ids.write({'lw_cc_fee_line': True})
        return fee_invoice

    # ------------------------------------------------------------------
    # Token verdict persistence
    # ------------------------------------------------------------------

    def _post_process(self):
        """Persist the token verdict and settle the backend wizard's fee.

        Direct-flow transactions know the verdict only from the checkout
        BIN capture (kept in the session, never at rest). Copying it onto
        the token lets later token payments reuse the verdict without the
        customer re-entering card digits.

        Backend Pay-wizard settlement: the wizard already
        built the ``account.payment`` BEFORE the transaction was created
        (account_payment.py's ``_prepare_payment_transaction_vals`` sets
        ``payment_id`` at create time), so core's own account_payment
        ``_post_process`` never calls ``_create_payment()`` for these
        transactions (its gate is ``not tx.payment_id``) -- the portal
        path's rewired ``_create_payment`` override above never runs
        either. Apply the fee here instead. This runs after the card was
        charged (``tx.state == 'done'``) and before the wizard's own
        ``_reconcile_payments`` (called later in the same
        ``action_create_payments()`` chain, not from post-processing), so
        the invoice's grown residual is what gets reconciled.
        """
        super()._post_process()
        for tx in self:
            if (
                tx.state == 'done'
                and tx.lw_cc_surcharge_bin_check
                and tx.token_id
                and not tx.token_id.lw_cc_surcharge_bin_check
            ):
                tx.token_id.sudo().lw_cc_surcharge_bin_check = (
                    tx.lw_cc_surcharge_bin_check
                )
            if (
                tx.state == 'done'
                and tx.lw_cc_surcharge_fee_amount
                and tx.payment_id
                and tx.invoice_ids
                and not tx.lw_cc_surcharge_fee_line_id
            ):
                tx._lw_cc_apply_cc_fee()
