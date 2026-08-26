# -*- coding: utf-8 -*-
"""payment.token override: persist the BIN card-type verdict.

The verdict is written once by payment.transaction._post_process after a
successful direct-flow (card-entry) payment, so later token payments can
uplift without the customer re-entering card digits. No card digits are
ever stored: only the card_type verdict.
"""
from odoo import fields, models


class PaymentToken(models.Model):
    _inherit = 'payment.token'

    lw_cc_surcharge_bin_check = fields.Char(
        string="BIN Check Result",
        readonly=True,
        copy=False,
        help=(
            "Card type from BIN lookup: 'CREDIT', 'DEBIT', or empty. "
            "Empty (all legacy tokens) means unknown: no portal fee "
            "uplift is applied for this token."
        ),
    )
