# -*- coding: utf-8 -*-
"""Per-customer opt-out flags for the monthly service charge and the
credit card surcharge."""
from odoo import fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    lw_cc_service_charge_optout = fields.Boolean(
        string="Exempt from Charge Terms Interest",
        default=False,
        copy=False,
        tracking=True,
        help="Default off: every customer with a past-due balance is "
             "subject to the monthly Charge Terms Interest service "
             "charge. Tick this to exempt this customer from the "
             "charge. The runner reads this flag off the commercial "
             "partner (the company); values on child contacts are "
             "ignored. Separate from the credit card surcharge "
             "opt-out (see lw_cc_surcharge_optout).",
    )
    lw_cc_surcharge_optout = fields.Boolean(
        string="Exempt from Credit Card Surcharge",
        default=False,
        copy=False,
        tracking=True,
        help="Default off: every customer paying by credit card is "
             "subject to the credit card surcharge fee. Tick this to "
             "exempt this customer from the fee -- no fee is quoted "
             "and no fee is charged. Every credit-card-surcharge gate "
             "reads this flag off the commercial partner (the "
             "company); values on child contacts are ignored. "
             "Separate from the Charge Terms Interest opt-out (see "
             "lw_cc_service_charge_optout); this flag starts False "
             "and does not copy the interest opt-out's value.",
    )
