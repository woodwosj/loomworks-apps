# -*- coding: utf-8 -*-
"""Company-scoped configuration for CC surcharge and monthly service charge.

All fields default to the DRY-RUN-ARMED shipping policy (19.0.5.0.0;
before that the module shipped INERT):
  - enabled = True
  - dry_run = True
  - cron ships ACTIVE, first tick on the 1st of the month at 06:00 UTC

Armed means the cron runs and writes its ``lw_cc.service.charge.run``
report every month. It does NOT mean money moves: with dry_run True the
runner creates zero accounting objects. Going live needs a dedicated
Service Charge Income Account plus an explicit dry-run untick, and
``_check_live_mode_requires_income_account`` below refuses the second
without the first.

The credit card surcharge is unaffected by the master switch
flipping to True: every surcharge surface also requires
``lw_cc_surcharge_cc_pct > 0`` (ships 0.0) and a non-empty
``lw_cc_surcharge_applicable_term_ids`` (ships empty), and both fail
closed.

Accounting configures these via the Settings UI (see
``views/res_config_settings_views.xml``) or directly on the company
form (see ``views/res_company_views.xml``).
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ResCompany(models.Model):
    _inherit = 'res.company'

    # ------------------------------------------------------------------
    # Master toggle + safety
    # ------------------------------------------------------------------
    lw_cc_surcharge_enabled = fields.Boolean(
        string="Enable CC Surcharge & Service Charge",
        default=True,
        tracking=True,
        help=(
            "Master switch for the entire module. When False, no "
            "surcharge or service charge logic runs regardless of "
            "other settings. Ships True as of 19.0.5.0.0 (DRY-RUN-ARMED "
            "posture): the monthly cron runs and reports, but creates "
            "no charge invoices while Dry-Run Mode below is ticked."
        ),
    )
    lw_cc_surcharge_dry_run = fields.Boolean(
        string="Service Charge Dry-Run Mode",
        default=True,
        tracking=True,
        help=(
            "When True, the monthly service charge cron computes "
            "charges and logs them but does NOT create charge invoices. "
            "Flip to False only after counsel sign-off and staging "
            "verification."
        ),
    )

    # ------------------------------------------------------------------
    # Service charge parameters (Track B)
    # ------------------------------------------------------------------
    lw_cc_surcharge_pct = fields.Float(
        string="Monthly Service Charge %",
        default=1.5,
        tracking=True,
        help=(
            "Percentage of past-due balance charged monthly. "
            "Per credit terms: the lesser of this rate or the maximum "
            "rate permissible by law."
        ),
    )
    lw_cc_surcharge_min_balance = fields.Monetary(
        string="Minimum Past-Due Balance",
        default=10.0,
        currency_field='currency_id',
        tracking=True,
        help=(
            "Partners whose total past-due residual is below this "
            "threshold are skipped. Prevents cent-level charges."
        ),
    )
    lw_cc_surcharge_past_due_days = fields.Integer(
        string="Past-Due Grace Days",
        default=30,
        tracking=True,
        help=(
            "Number of days past the invoice due date before an "
            "invoice qualifies for service charge. Default 30 means "
            "invoices more than 30 days overdue are charged."
        ),
    )

    # ------------------------------------------------------------------
    # Surcharge parameters
    # ------------------------------------------------------------------
    lw_cc_surcharge_cc_pct = fields.Float(
        string="Credit Card Surcharge %",
        default=0.0,
        tracking=True,
        help=(
            "Percentage added to credit card payments from Net 30+ "
            "customers. Set to 0 until the payment flow "
            "integration is live."
        ),
    )

    # ------------------------------------------------------------------
    # Product + income account mapping
    # ------------------------------------------------------------------
    lw_cc_surcharge_product_id = fields.Many2one(
        'product.product',
        string="Service Charge Product",
        default=lambda self: self.env.ref(
            'lw_cc_surcharge.product_service_charge',
            raise_if_not_found=False,
        ),
        tracking=True,
        help=(
            "Product used for the invoice line on monthly service "
            "charge invoices. Defaults to the module's seed product."
        ),
    )
    lw_cc_surcharge_income_account_id = fields.Many2one(
        'account.account',
        string="Service Charge Income Account",
        tracking=True,
        help=(
            "Dedicated income account for service charge revenue. "
            "Must NOT be COGS. Required before the cron can create "
            "charge invoices."
        ),
    )
    lw_cc_surcharge_cc_income_account_id = fields.Many2one(
        'account.account',
        string="CC Surcharge Income Account",
        tracking=True,
        help=(
            "Dedicated income account for credit card surcharge "
            "revenue."
        ),
    )

    # ------------------------------------------------------------------
    # Applicable payment terms (Net 30+)
    # ------------------------------------------------------------------
    lw_cc_surcharge_applicable_term_ids = fields.Many2many(
        'account.payment.term',
        string="Applicable Payment Terms (Net 30+)",
        tracking=True,
        help=(
            "Only customers on these payment terms are subject to "
            "service charges and credit card surcharges. Net 0 "
            "customers are excluded."
        ),
    )

    # ------------------------------------------------------------------
    # Charge Terms Interest delivery mode
    # ------------------------------------------------------------------
    lw_cc_sc_mode = fields.Selection(
        [
            ('separate_invoice', "Separate Monthly Invoice (current)"),
            ('invoice_line', "Line on Existing Past-Due Invoice"),
        ],
        string="Charge Terms Interest Mode",
        default='separate_invoice',
        tracking=True,
        help=(
            "How the monthly Charge Terms Interest is delivered. "
            "'Separate Monthly Invoice' (default, current behavior) "
            "creates one dedicated SC invoice per partner per month. "
            "'Line on Existing Past-Due Invoice' adds an interest line "
            "directly to each qualifying past-due invoice instead -- it "
            "ages with the principal and matches monthly reporting. "
            "Ships defaulting to the current behavior; flip only after "
            "a clean staging dry-run tick."
        ),
    )
    lw_cc_sc_compounding = fields.Selection(
        [
            ('simple', "Simple (interest excluded from next month's base)"),
            ('compound', "Compound (residual, including prior interest, is the base)"),
        ],
        string="Charge Terms Interest Compounding",
        default='compound',
        tracking=True,
        help=(
            "Only consulted in 'Line on Existing Past-Due Invoice' mode. "
            "'Compound' bases each month's interest on the invoice's full "
            "residual (prior months' interest lines are already part of "
            "it). 'Simple' subtracts the total of prior interest lines "
            "from the residual first, so interest never compounds. "
            "Compounding legality is a counsel sign-off gate before "
            "go-live -- see the design notes."
        ),
    )
    lw_cc_sc_partial_policy = fields.Selection(
        [
            ('skip_partial', "Skip Partially-Paid Invoices"),
            ('rereconcile', "Add Line & Re-Reconcile Existing Payments"),
        ],
        string="Charge Terms Interest Partial-Payment Policy",
        default='rereconcile',
        tracking=True,
        help=(
            "Only consulted in 'Line on Existing Past-Due Invoice' mode. "
            "'Skip Partially-Paid Invoices' never touches an invoice "
            "whose payment_state is 'partial'. 'Add Line & Re-Reconcile' "
            "adds the interest line and restores the existing partial "
            "payment's reconciliation afterward (see "
            "_lw_cc_add_charge_line's allow_partial contract)."
        ),
    )

    # ------------------------------------------------------------------
    # Dry-run semantics (shared by BOTH money paths)
    # ------------------------------------------------------------------

    def _lw_cc_fee_is_dry_run(self):
        """Whether the credit card fee must be computed but never charged.

        THE SINGLE SOURCE OF TRUTH for dry-run on the card-fee side, so
        the five decision sites that gate the fee can never drift apart:

          * ``lw_cc_surcharge/models/payment_transaction.py``
            ``_assess_cc_surcharge`` (legacy post-payment engine)
          * ``lw_cc_surcharge`` portal quote gates
            (``_lw_cc_quote_gates_fail``, ``_lw_cc_quote_overdue``)
          * ``lw_cc_surcharge``
            ``_lw_cc_uplift_transaction_kwargs`` (the checkout-time
            uplift -- the one that decides what the card is charged)
          * ``lw_cc_surcharge``
            ``_lw_cc_surcharge_gates_pass`` (backend Pay wizard)

        HISTORY, because it matters to anyone auditing this. Until
        19.0.5.0.0 ``lw_cc_surcharge_dry_run`` was read in exactly
        ONE place in the entire codebase -- the monthly service charge
        runner. The card fee ignored it completely and gated only on
        ``lw_cc_surcharge_enabled``, ``lw_cc_surcharge_cc_pct``
        and the applicable payment terms. That was survivable only
        because the percentage ships at 0.0 and the terms list ships
        empty, and both fail closed. It stopped being survivable the
        moment this module started shipping with the master switch ON:
        the configuration work consist of precisely configuring terms and setting a
        percentage, and on the day someone did that, card fees would
        have started hitting live customer payments while Settings and
        the runbook both said "dry-run". Dry-run would have been a
        label that protected one of the two money paths.

        WHERE THE GATE HAS TO SIT, which is not the same for the two
        paths. The legacy engine charges nothing to the card and
        creates a separate invoice AFTER the fact, so it can be stopped
        at the point of writing. The portal uplift and the backend
        wizard add the fee to the amount that is charged to the card
        BEFORE settlement, so they must be stopped where the fee is
        DECIDED (quote / uplift / wizard gate), never at settlement:
        suppressing the invoice at settlement would take the customer's
        money and record nothing.
        """
        self.ensure_one()
        return bool(self.lw_cc_surcharge_dry_run)

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------

    @api.constrains('lw_cc_surcharge_pct')
    def _check_surcharge_pct(self):
        """Prevent accidental misconfiguration (e.g. 150 instead of 1.5).

        Caps at 5% to catch typos while allowing jurisdictions that
        permit higher rates to override via direct DB write if needed.
        """
        for company in self:
            pct = company.lw_cc_surcharge_pct
            if pct < 0 or pct > 5:
                raise ValidationError(_(
                    "Monthly Service Charge %% must be between 0 and 5 "
                    "(got %.2f). Check for decimal point errors "
                    "(e.g. 150 instead of 1.5).",
                    pct,
                ))

    @api.constrains('lw_cc_surcharge_past_due_days')
    def _check_past_due_days(self):
        """Grace days cannot be negative (would include future-due invoices)."""
        for company in self:
            if company.lw_cc_surcharge_past_due_days < 0:
                raise ValidationError(_(
                    "Past-Due Grace Days cannot be negative "
                    "(got %d). Use 0 to charge from day 1 past due.",
                    company.lw_cc_surcharge_past_due_days,
                ))

    @api.constrains('lw_cc_surcharge_cc_pct')
    def _check_cc_pct(self):
        """CC surcharge percentage sanity bound."""
        for company in self:
            pct = company.lw_cc_surcharge_cc_pct
            if pct < 0 or pct > 10:
                raise ValidationError(_(
                    "Credit Card Surcharge %% must be between 0 and 10 "
                    "(got %.2f).",
                    pct,
                ))

    @api.constrains('lw_cc_surcharge_enabled', 'lw_cc_surcharge_dry_run',
                    'lw_cc_surcharge_income_account_id')
    def _check_live_mode_requires_income_account(self):
        """Live mode (enabled + dry-run off) requires an income account.

        The runner falls back to the product's income account with only
        a warning log; acceptable for dry-run review, not for live
        posting of charge invoices.
        """
        for company in self:
            if (company.lw_cc_surcharge_enabled
                    and not company.lw_cc_surcharge_dry_run
                    and not company.lw_cc_surcharge_income_account_id):
                raise ValidationError(_(
                    "Service charge live mode requires a dedicated "
                    "Service Charge Income Account. Set it in Settings "
                    "> CC Surcharge before turning off dry-run."
                ))

# ===================================================================
# Below: merged from the portal-uplift companion module: res_company.py
# (portal uplift + backend wizard arming flags).
# ===================================================================
# -*- coding: utf-8 -*-
"""res.company overrides: the two uplift arming flags.

The portal uplift flag ships False (INERT): until Accounting arms it,
the portal quote/charge path changes nothing. The backend Pay-wizard
flag ships True (operator decision): the staff auto-tick
checkbox goes live with the surcharge itself, so there is no second
go-live step for staff-side surcharging. Both flags are inert on their
own either way -- the wizard gate also requires the surcharge to be
enabled with a positive percentage, and those still ship off.
"""
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    lw_cc_surcharge_portal_uplift = fields.Boolean(
        string="Portal Fee Uplift",
        default=False,
        tracking=True,
        help=(
            "Show and charge the credit card fee in the portal payment "
            "modal as ONE transaction (invoice + fee). When False the "
            "portal flow is untouched and the legacy post-payment "
            "surcharge engine of lw_cc_surcharge applies."
        ),
    )
    lw_cc_surcharge_backend_wizard = fields.Boolean(
        string="Backend Pay-Wizard Surcharge",
        default=True,
        tracking=True,
        help=(
            "Show the 'Apply Credit Card Surcharge' checkbox in the "
            "backend Pay wizard (Register Payment) when a saved card "
            "token is selected, pre-checked for credit tokens and "
            "waivable by staff. Ships ON: when the surcharge itself is "
            "enabled, staff-side card payments charge the fee by "
            "default and staff can untick to waive. When False the "
            "wizard is untouched and backend token payments get no "
            "surcharge at all -- the coverage gap this closes (the "
            "legacy engine is suppressed for invoice-linked "
            "transactions once the portal uplift is armed, and the "
            "uplift itself only lives in the portal controller)."
        ),
    )
