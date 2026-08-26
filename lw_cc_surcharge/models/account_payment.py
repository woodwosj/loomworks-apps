# -*- coding: utf-8 -*-
"""account.payment extension: thread the backend wizard's CC fee onto the
payment transaction.

The wizard's own ``_create_payment_vals_from_wizard`` override computes
the fee and stores it here -- it MUST be a stored field, not a context
key, since core clears the wizard's context before the payment record
that eventually creates the transaction is used
(account_payment_register.py, ``_create_payments`` -> ``_init_payments``
-> ``action_post``; the wizard's own transient env does not survive
that hop). ``_prepare_payment_transaction_vals`` then copies the fee,
together with the selected token's BIN verdict, onto the
``payment.transaction`` about to charge the token, so the SAME single
transaction charges base + fee (matching the portal uplift's contract
of one transaction = invoice + fee).
"""
from odoo import fields, models


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    lw_cc_surcharge_fee_amount = fields.Monetary(
        string="CC Surcharge Fee",
        currency_field='currency_id',
        readonly=True,
        copy=False,
        help=(
            "Credit card fee applied by the backend Pay-wizard checkbox "
            "included in this payment's amount. Zero means "
            "no fee was applied (checkbox unavailable or waived)."
        ),
    )
    lw_cc_surcharge_wizard_payment = fields.Boolean(
        string="Created Via CC Surcharge Pay Wizard",
        readonly=True,
        copy=False,
        help=(
            "True when this payment was created through the standard "
            "Pay wizard (account.payment.register), as opposed to a "
            "payment created directly on its own form. Stamped "
            "unconditionally by the wizard -- regardless of whether a "
            "fee actually applied (unavailable gate, or staff waived "
            "it) -- so it is a pure PROVENANCE marker, not a "
            "fee-was-charged marker. Used ONLY to narrow "
            "payment.transaction._assess_cc_surcharge's backend-wizard "
            "suppression: a direct account.payment form token payment "
            "(the documented 'known gap' -- fee-less by design) must "
            "still fall through to the legacy engine even when the "
            "backend wizard flag is armed; only wizard-owned "
            "transactions are suppressed."
        ),
    )

    def _prepare_payment_transaction_vals(self, **extra_create_values):
        vals = super()._prepare_payment_transaction_vals(**extra_create_values)
        if self.lw_cc_surcharge_fee_amount and self.payment_token_id:
            vals.setdefault(
                'lw_cc_surcharge_fee_amount',
                self.lw_cc_surcharge_fee_amount,
            )
            vals.setdefault(
                'lw_cc_surcharge_bin_check',
                self.payment_token_id.lw_cc_surcharge_bin_check,
            )
        return vals
