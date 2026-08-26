# -*- coding: utf-8 -*-
"""account.payment.register extension: backend Pay-wizard CC surcharge
checkbox.

Gated on the company flag ``lw_cc_surcharge_backend_wizard``, which
ships ON (default True, seeded on existing companies; 
the staff auto-tick goes live with the surcharge itself.
The flag changes nothing by itself -- the gate below also requires the
surcharge to be enabled with a positive percentage. Closes a real
coverage gap: before this, backend token payments got NO surcharge at
all -- the legacy post-payment engine is suppressed for invoice-linked
transactions once the portal uplift flag is armed, and the uplift
itself lives only in the portal controller (see the design notes exploration
finding B).

"Buttons hide, they don't authorize": the checkbox here only pre-fills a
default and lets staff waive. Every gate is RE-EVALUATED server-side in
``_create_payment_vals_from_wizard`` -- a forged/tampered
``lw_cc_apply_cc_surcharge`` is silently ignored whenever the
server-side re-check disagrees; no fee is added and no error is raised.
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class AccountPaymentRegister(models.TransientModel):
    _inherit = 'account.payment.register'

    lw_cc_surcharge_available = fields.Boolean(
        string="CC Surcharge Available",
        compute='_compute_lw_cc_surcharge_available',
        help=(
            "Technical field gating the surcharge checkbox's visibility: "
            "the backend wizard flag is armed, the surcharge is enabled "
            "with a percentage set, exactly one posted invoice is being "
            "registered on an applicable payment term, the customer is "
            "not opted out, a payment token is selected whose BIN "
            "verdict is not DEBIT, the invoice is not in an "
            "early-payment-discount state, and the payment currency is "
            "the company currency."
        ),
    )
    lw_cc_apply_cc_surcharge = fields.Boolean(
        string="Apply Credit Card Surcharge",
        compute='_compute_lw_cc_apply_cc_surcharge',
        store=True,
        readonly=False,
        help=(
            "Pre-checked when the selected token's BIN verdict is "
            "CREDIT. Untick to waive the fee for this payment. Tokens "
            "with no stored verdict (saved outside the BIN-capture "
            "form) default UNCHECKED -- staff may still tick it "
            "manually."
        ),
    )
    lw_cc_surcharge_fee_display = fields.Monetary(
        string="CC Surcharge Fee",
        compute='_compute_lw_cc_surcharge_fee_display',
        currency_field='currency_id',
    )

    # ------------------------------------------------------------------
    # Compute methods
    # ------------------------------------------------------------------

    @api.depends(
        'payment_token_id', 'journal_id', 'can_edit_wizard', 'amount',
        'currency_id', 'company_id', 'line_ids',
    )
    def _compute_lw_cc_surcharge_available(self):
        for wizard in self:
            wizard.lw_cc_surcharge_available = (
                wizard._lw_cc_surcharge_gates_pass()
            )

    @api.depends('payment_token_id', 'lw_cc_surcharge_available')
    def _compute_lw_cc_apply_cc_surcharge(self):
        for wizard in self:
            if not wizard.lw_cc_surcharge_available:
                wizard.lw_cc_apply_cc_surcharge = False
                continue
            # Empty-verdict tokens (saved outside the BIN-capture form)
            # default UNCHECKED -- fail closed, staff can still assert
            # manually (gate #5 in the design notes).
            wizard.lw_cc_apply_cc_surcharge = (
                wizard.payment_token_id.lw_cc_surcharge_bin_check == 'CREDIT'
            )

    @api.depends(
        'lw_cc_surcharge_available', 'lw_cc_apply_cc_surcharge', 'amount',
        'company_id',
    )
    def _compute_lw_cc_surcharge_fee_display(self):
        for wizard in self:
            if not (
                wizard.lw_cc_surcharge_available
                and wizard.lw_cc_apply_cc_surcharge
            ):
                wizard.lw_cc_surcharge_fee_display = 0.0
                continue
            wizard.lw_cc_surcharge_fee_display = (
                wizard.env['payment.transaction']._lw_cc_compute_portal_fee(
                    wizard.amount, wizard.company_id,
                    currency=wizard.currency_id,
                )
            )

    # ------------------------------------------------------------------
    # Gate evaluation -- shared by the client-visible compute AND the
    # server-side re-validation, so the two can never drift apart.
    # ------------------------------------------------------------------

    def _lw_cc_surcharge_gates_pass(self):
        """Evaluate every backend-wizard CC surcharge gate.

        Called both from ``_compute_lw_cc_surcharge_available``
        (defaulting/visibility) and from
        ``_create_payment_vals_from_wizard`` (server-side re-check
        before the fee is actually added) -- the SAME method, so a
        forged client value can never diverge from what the server
        would have computed itself.
        """
        self.ensure_one()
        company = self.company_id
        if not (
            company.lw_cc_surcharge_backend_wizard
            and company.lw_cc_surcharge_enabled
            and (company.lw_cc_surcharge_cc_pct or 0.0) > 0.0
        ):
            return False
        # Dry-run: the fee is added to the amount charged to the card
        # (see _create_payment_vals_from_wizard), so it must be stopped
        # here, where the wizard DECIDES, and never at settlement. This
        # gate feeds both the checkbox's visibility/default and the
        # server-side re-check, so in dry-run staff never see the
        # checkbox offered and a forged client value cannot charge the
        # fee either. See res_company._lw_cc_fee_is_dry_run.
        if company._lw_cc_fee_is_dry_run():
            return False
        if not self.payment_token_id:
            return False
        if self.payment_token_id.lw_cc_surcharge_bin_check == 'DEBIT':
            return False
        if self.currency_id != company.currency_id:
            return False

        invoices = self._lw_cc_surcharge_target_invoices()
        if len(invoices) != 1:
            return False
        invoice = invoices
        if invoice.state != 'posted' or invoice.move_type != 'out_invoice':
            return False

        applicable_terms = company.lw_cc_surcharge_applicable_term_ids
        if (
            not applicable_terms
            or invoice.invoice_payment_term_id not in applicable_terms
        ):
            return False
        if invoice.commercial_partner_id.lw_cc_surcharge_optout:
            return False

        next_values = invoice._get_invoice_next_payment_values()
        if next_values.get('installment_state') == 'epd':
            return False

        return True

    def _lw_cc_surcharge_target_invoices(self):
        """The posted customer invoice(s) this wizard is registering payment for."""
        self.ensure_one()
        return self.line_ids.move_id.filtered(
            lambda m: m.move_type == 'out_invoice'
        )

    # ------------------------------------------------------------------
    # Payment creation: fee amount + stored field
    # ------------------------------------------------------------------

    def _create_payment_vals_from_wizard(self, batch_result):
        """Add the surcharge fee to the payment amount, gates re-checked.

        The checkbox is presentation only: whatever the client sent for
        ``lw_cc_apply_cc_surcharge`` is irrelevant here except as intent
        -- ``_lw_cc_surcharge_gates_pass()`` is re-run from scratch
        against the wizard's current server-side state, and a fee is
        only ever added when THAT re-check passes. A caller that forges
        ``lw_cc_apply_cc_surcharge=True`` while the gates say
        unavailable gets a payment with no fee added and no error --
        the checkbox simply had no effect.
        """
        payment_vals = super()._create_payment_vals_from_wizard(batch_result)
        # Stamped UNCONDITIONALLY, before any of the fee-specific early
        # returns below: this is a provenance marker ("built by THIS
        # wizard"), not a fee-was-charged marker. A waived checkbox or a
        # failed gate re-check still means the payment came from the
        # wizard, which is exactly what
        # payment.transaction._assess_cc_surcharge's suppression needs
        # to distinguish from a direct account.payment form token
        # payment (never touches this method at all).
        payment_vals['lw_cc_surcharge_wizard_payment'] = True
        if not self.lw_cc_apply_cc_surcharge:
            return payment_vals
        if not self._lw_cc_surcharge_gates_pass():
            _logger.warning(
                "lw_cc_surcharge: backend wizard fee "
                "checkbox was set but the gates fail on server-side "
                "re-check (partner %s); no fee applied.",
                self.partner_id.display_name,
            )
            return payment_vals

        base_amount = payment_vals.get('amount', self.amount)
        fee = self.env['payment.transaction']._lw_cc_compute_portal_fee(
            base_amount, self.company_id, currency=self.currency_id,
        )
        if fee <= 0.0:
            return payment_vals

        payment_vals['amount'] = base_amount + fee
        payment_vals['lw_cc_surcharge_fee_amount'] = fee
        return payment_vals

    # ------------------------------------------------------------------
    # Settle a stranded fallback CCS invoice against the SAME
    # payment that already collected its fee
    # ------------------------------------------------------------------

    def _reconcile_payments(self, to_process, edit_mode=False):
        """Reconcile the payments, then settle any stranded CC-fee
        fallback invoice against the SAME payment that already
        collected it.

        This can only run AFTER core's reconcile above, and specifically
        cannot run any earlier in the chain (e.g. from
        ``_post_process`` itself): the payment's own account.move is
        not posted until ``account.payment.action_post()`` returns, and
        ``_post_process`` -- where ``_lw_cc_apply_cc_fee`` runs --
        executes BEFORE that posting step completes (verified in real
        v19 source, account_payment/models/account_payment.py's
        ``action_post()``: ``transactions._post_process()`` is called,
        THEN ``super(AccountPayment, payments_tx_done).action_post()``
        actually posts the payment's move). There is no payment
        receivable line to match against any earlier than here.

        See ``_lw_cc_reconcile_stranded_cc_fallback`` for the actual
        settlement logic -- split out so it is directly callable (and
        testable) without re-driving core's own reconcile above.
        """
        super()._reconcile_payments(to_process, edit_mode=edit_mode)
        self._lw_cc_reconcile_stranded_cc_fallback(to_process)

    def _lw_cc_reconcile_stranded_cc_fallback(self, to_process):
        """Match a stranded fallback CCS invoice against its payment's
        leftover residual.

        Core's own reconcile (in ``_reconcile_payments`` above) matches
        the payment's receivable line against ``vals['to_reconcile']``
        -- the ORIGINAL invoice's term lines, captured when this wizard
        opened, long before ``_lw_cc_apply_cc_fee`` ever ran. When that
        same-invoice write failed (locked period, hash-locked journal,
        ...) and ``_lw_cc_apply_cc_fee`` fell back to a separate CCS
        invoice (``payment.transaction`` on the backend Pay-wizard
        path, ``payment_transaction.py``'s ``_post_process``), that
        fallback invoice's own receivable line was never in
        ``to_reconcile`` and core's match leaves an EXACTLY matching
        amount of credit unreconciled on the payment's own receivable
        line (base+fee was charged; only base got matched against the
        original invoice). Match the two here so neither is left open.
        """
        for vals in to_process:
            payment = vals['payment']
            tx = payment.payment_transaction_id
            if not (
                tx
                and tx.lw_cc_surcharge_fee_amount
                and not tx.lw_cc_surcharge_fee_line_id
            ):
                continue  # no fee, or the fee settled on the SAME
                          # invoice -- core's reconcile above already
                          # covers that case (the invoice's own
                          # residual grew to match the payment).

            fallback_invoice = tx._lw_cc_find_fallback_cc_fee_invoice()
            if not fallback_invoice:
                continue  # the fallback invoice itself failed to be
                          # created (logged as an error elsewhere) --
                          # nothing here to reconcile.

            payment_lines = payment.move_id.line_ids.filtered(
                lambda l: (
                    l.account_id.account_type == 'asset_receivable'
                    and not l.reconciled
                )
            )
            fallback_lines = fallback_invoice.line_ids.filtered(
                lambda l: (
                    l.account_id.account_type == 'asset_receivable'
                    and not l.reconciled
                )
            )
            # Grouped by account like core's own _reconcile_payments
            # above -- the fallback invoice is for the SAME commercial
            # partner as the original invoice, so its receivable
            # account matches by construction, but never assume it.
            for account in payment_lines.account_id & fallback_lines.account_id:
                (payment_lines + fallback_lines).filtered(
                    lambda l, account=account: l.account_id == account
                ).reconcile()
