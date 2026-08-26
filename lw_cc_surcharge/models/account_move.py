# -*- coding: utf-8 -*-
"""account.move extension for service charge invoice creation.

Provides ``_create_service_charge_invoice()`` which creates and posts
a single customer invoice for the monthly service charge on past-due
balances. Called by ``lw_cc_service_charge_runner._run_monthly_service_charge()``.

The logic follows the standard AR dunning pattern:
  - Per-commercial-partner grouping
  - Savepoint isolation
  - Chatter audit via ``subtype_xmlid='mail.mt_note'`` (internal note,
    avoids customer-subscription email loop)
  - Per-customer opt-out gate: ``lw_cc_service_charge_optout`` on the
    commercial partner (invoice-level ``dunning_hold`` still excludes
    disputed invoices)
"""
import logging

from odoo import Command, _, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_compare, float_round

_logger = logging.getLogger(__name__)

SERVICE_CHARGE_CHATTER_MARKER = 'lw_cc_surcharge:service_charge'


class AccountMove(models.Model):
    _inherit = 'account.move'

    # ------------------------------------------------------------------
    # Service charge invoice creation
    # ------------------------------------------------------------------

    def _create_service_charge_invoice(
        self, partner, charge_amount, company, source_invoices,
    ):
        """Create and post one service charge invoice for a partner.

        Creates an ``out_invoice`` with a single line (the service charge
        product), priced at ``charge_amount``. The invoice is posted
        immediately so it appears on the partner's AR.

        :param partner: ``res.partner`` (commercial partner).
        :param charge_amount: float amount to charge.
        :param company: ``res.company`` for multi-company scoping.
        :param source_invoices: ``account.move`` recordset of the past-due
            invoices that generated this charge (for chatter logging).
        :returns: The created ``account.move`` or empty recordset on failure.
        """
        product = (
            company.lw_cc_surcharge_product_id
            or self.env.ref(
                'lw_cc_surcharge.product_service_charge',
                raise_if_not_found=False,
            )
        )
        if not product:
            _logger.error(
                "lw_cc_surcharge: no service charge product configured "
                "for company %s; cannot create charge invoice for %s.",
                company.display_name, partner.display_name,
            )
            return self.env['account.move']

        income_account = company.lw_cc_surcharge_income_account_id
        if not income_account:
            # Fall back to the product's income account if no dedicated
            # account is configured. Log a warning so Accounting knows
            # to set one.
            income_account = (
                product.property_account_income_id
                or product.categ_id.property_account_income_categ_id
            )
            if income_account:
                _logger.warning(
                    "lw_cc_surcharge: no dedicated income account set; "
                    "using product default %s for %s charge invoice.",
                    income_account.display_name, partner.display_name,
                )
            else:
                _logger.error(
                    "lw_cc_surcharge: no income account available "
                    "(neither configured nor product default) for %s; "
                    "cannot create charge invoice.",
                    partner.display_name,
                )
                return self.env['account.move']

        # Round to currency precision.
        charge_amount = float_round(
            charge_amount,
            precision_rounding=company.currency_id.rounding,
        )

        # Skip zero or negative charges.
        if float_compare(
            charge_amount, 0.0,
            precision_rounding=company.currency_id.rounding,
        ) <= 0:
            _logger.info(
                "lw_cc_surcharge: computed charge for %s is %.2f "
                "(<= 0 after rounding); skipping.",
                partner.display_name, charge_amount,
            )
            return self.env['account.move']

        total_residual = sum(source_invoices.mapped('amount_residual'))

        today = fields.Date.context_today(self)
        invoice_vals = {
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'company_id': company.id,
            'currency_id': company.currency_id.id,
            'invoice_date': today,
            # SC/<YYYY-MM>/<partner id>: the runner's idempotency domain
            # matches this prefix, so the month format must stay in sync
            # with _partner_already_charged_this_month().
            'ref': "SC/%s/P%s" % (today.strftime("%Y-%m"), partner.id),
            'invoice_origin': ", ".join(source_invoices.mapped('name')),
            'invoice_line_ids': [Command.create({
                'product_id': product.id,
                'name': _(
                    "Monthly Service Charge on Past-Due Balance "
                    "(%(pct).2f%% per terms)",
                    pct=company.lw_cc_surcharge_pct,
                ),
                'quantity': 1,
                'price_unit': charge_amount,
                'account_id': income_account.id,
                'tax_ids': [Command.clear()],
            })],
            'narration': _(
                "Auto-generated monthly service charge on past-due "
                "balance. %(count)d overdue invoice(s) totaling "
                "%(total)s at %(pct).2f%% per terms.",
                count=len(source_invoices),
                total=f"{total_residual:,.2f}",
                pct=company.lw_cc_surcharge_pct,
            ),
        }

        # Apply payment term if the partner has one.
        if partner.property_payment_term_id:
            invoice_vals['invoice_payment_term_id'] = (
                partner.property_payment_term_id.id
            )

        charge_invoice = self.env['account.move'].with_company(
            company,
        ).sudo().create(invoice_vals)

        # Post immediately so the charge shows on AR.
        charge_invoice.action_post()

        _logger.info(
            "lw_cc_surcharge: created and posted service charge "
            "invoice %s for %s, amount %.2f (%d source invoices).",
            charge_invoice.name, partner.display_name, charge_amount,
            len(source_invoices),
        )

        # Chatter log on each source invoice.
        for src in source_invoices:
            src.with_context(tracking_disable=True).message_post(
                body=_(
                    "%(marker)s: service charge of %(amount)s assessed "
                    "(charge invoice %(inv)s).",
                    marker=SERVICE_CHARGE_CHATTER_MARKER,
                    amount=f"{charge_amount:.2f}",
                    inv=charge_invoice.name or '(draft)',
                ),
                subtype_xmlid='mail.mt_note',
            )

        return charge_invoice


# ------------------------------------------------------------------
# CC Surcharge invoice creation
# ------------------------------------------------------------------

CC_SURCHARGE_CHATTER_MARKER = 'lw_cc_surcharge:cc_surcharge'


class AccountMoveCCSurcharge(models.Model):
    """Extension for CC surcharge invoice creation.

    Defined as a separate class to avoid cluttering the service charge
    class, but still inherits ``account.move``.
    """
    _inherit = 'account.move'

    def _create_cc_surcharge_invoice(
        self, partner, surcharge_amount, company, source_invoices, transaction,
    ):
        """Create and post a CC surcharge invoice for a single transaction.

        :param partner: ``res.partner`` (commercial partner).
        :param surcharge_amount: float amount to charge.
        :param company: ``res.company``.
        :param source_invoices: ``account.move`` recordset of invoices paid.
        :param transaction: ``payment.transaction`` that triggered this.
        :returns: The created ``account.move`` or empty recordset on failure.
        """
        # Use the service charge product if no CC-specific product is set.
        product = (
            company.lw_cc_surcharge_product_id
            or self.env.ref(
                'lw_cc_surcharge.product_service_charge',
                raise_if_not_found=False,
            )
        )
        if not product:
            _logger.error(
                "lw_cc_surcharge: no product configured for CC surcharge "
                "invoice for company %s; skipping.",
                company.display_name,
            )
            return self.env['account.move']

        # Use the CC income account, falling back to service charge account.
        income_account = (
            company.lw_cc_surcharge_cc_income_account_id
            or company.lw_cc_surcharge_income_account_id
            or product.property_account_income_id
            or product.categ_id.property_account_income_categ_id
        )
        if not income_account:
            _logger.error(
                "lw_cc_surcharge: no income account for CC surcharge; "
                "skipping invoice for %s.",
                partner.display_name,
            )
            return self.env['account.move']

        surcharge_amount = float_round(
            surcharge_amount,
            precision_rounding=company.currency_id.rounding,
        )
        if float_compare(
            surcharge_amount, 0.0,
            precision_rounding=company.currency_id.rounding,
        ) <= 0:
            return self.env['account.move']

        # Recover the fee's own base for display -- NOT from
        # transaction.amount (adverse review, class defect for
        # one caller of this shared method and not the other, same
        # seam class). transaction.amount means different
        # things depending on caller: the legacy engine's own
        # _assess_cc_surcharge computes
        # `surcharge_amount = self.amount * (cc_pct / 100.0)` where
        # self.amount genuinely IS the base (the fee is a separate,
        # never-combined invoice) -- verified by reading
        # payment_transaction.py directly. lw_cc_surcharge_
        # the uplift fallback (_lw_cc_fallback_cc_fee_invoice) calls
        # this SAME method with transaction=self where self.amount is
        # base+fee under the uplift (the card was already charged for
        # the combined total) -- verified by reading
        # _lw_cc_compute_portal_fee, which computes the identical
        # `fee = base * (cc_pct / 100.0)` relationship, so
        # transaction.lw_cc_surcharge_fee_amount (passed here as
        # surcharge_amount) and self.amount are NOT the same quantity
        # for that caller. Both callers use the IDENTICAL
        # fee = base * pct/100 relationship, though -- only what
        # transaction.amount itself represents differs -- so base can
        # be recovered exactly, and caller-agnostically, straight from
        # surcharge_amount and pct alone, never touching
        # transaction.amount: base = surcharge_amount / (pct/100).
        pct = company.lw_cc_surcharge_cc_pct
        if pct:
            fee_base = float_round(
                surcharge_amount / (pct / 100.0),
                precision_rounding=company.currency_id.rounding,
            )
        else:
            # pct is 0/unset at THIS moment (the company was
            # reconfigured between when the fee was computed and this
            # invoice-creation call) -- cannot reconstruct base by
            # division. transaction.amount is at least a real, not
            # fabricated, number to fall back to, even though its
            # relationship to "the base" is caller-dependent in this
            # corner case.
            fee_base = transaction.amount

        invoice_vals = {
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'company_id': company.id,
            'currency_id': company.currency_id.id,
            'invoice_date': fields.Date.context_today(self),
            # CCS/<tx reference>: ties the charge invoice to the payment
            # transaction that triggered it; invoice_origin traces the
            # invoices that were paid.
            'ref': "CCS/%s" % transaction.reference,
            'invoice_origin': ", ".join(source_invoices.mapped('name')),
            'invoice_line_ids': [Command.create({
                'product_id': product.id,
                'name': _(
                    "Credit Card Processing Fee (%(pct).2f%% of %(amount)s)",
                    pct=company.lw_cc_surcharge_cc_pct,
                    amount=f"{fee_base:.2f}",
                ),
                'quantity': 1,
                'price_unit': surcharge_amount,
                'account_id': income_account.id,
                'tax_ids': [Command.clear()],
            })],
            'narration': _(
                "Auto-generated CC surcharge for transaction %(tx)s "
                "(%(pct).2f%% of %(amount)s).",
                tx=transaction.reference,
                pct=company.lw_cc_surcharge_cc_pct,
                amount=f"{fee_base:.2f}",
            ),
        }

        if partner.property_payment_term_id:
            invoice_vals['invoice_payment_term_id'] = (
                partner.property_payment_term_id.id
            )

        charge_invoice = self.env['account.move'].with_company(
            company,
        ).sudo().create(invoice_vals)
        charge_invoice.action_post()

        _logger.info(
            "lw_cc_surcharge: created and posted CC surcharge invoice "
            "%s for %s, amount %.2f (tx %s).",
            charge_invoice.name, partner.display_name, surcharge_amount,
            transaction.reference,
        )

        # Chatter log on each source invoice.
        for src in source_invoices:
            src.with_context(tracking_disable=True).message_post(
                body=_(
                    "%(marker)s: CC surcharge of %(amount)s assessed "
                    "(tx %(tx)s, charge invoice %(inv)s).",
                    marker=CC_SURCHARGE_CHATTER_MARKER,
                    amount=f"{surcharge_amount:.2f}",
                    tx=transaction.reference,
                    inv=charge_invoice.name or '(draft)',
                ),
                subtype_xmlid='mail.mt_note',
            )

        return charge_invoice


# ------------------------------------------------------------------
# Shared posted-invoice charge-line helper
# ------------------------------------------------------------------

CHARGE_LINE_CHATTER_MARKER = 'lw_cc_surcharge:charge_line'


class AccountMoveChargeLine(models.Model):
    """Generic "add a line to an already-posted invoice" helper.

    Both the same-invoice CC fee and the per-invoice interest
    line delegate to this. Kept free of CC-specific or
    interest-specific logic on purpose -- callers pass their own
    product/account/label and stash anything extra (e.g. an interest
    marker field) via ``extra_line_vals``.
    """
    _inherit = 'account.move'

    def _lw_cc_add_charge_line(
        self, product, amount, account, label,
        extra_line_vals=None, allow_partial=False,
    ):
        """Add one charge line to this posted customer invoice, in place.

        Never touches the move's ``date`` -- the line books into the
        invoice's original period by design (see the design notes hazard #2 on
        this choice's reporting consequences). Callers that need a
        different-period fallback (locked period, hash-locked journal)
        must catch the ``UserError`` this raises and fall back to a
        separate, dated-today invoice themselves.

        :param product: ``product.product`` for the new line.
        :param float amount: line amount (``price_unit``, quantity 1).
        :param account: ``account.account`` for the new line.
        :param str label: line ``name`` / description.
        :param dict extra_line_vals: additional ``account.move.line``
            create vals to merge onto the new line (e.g. an
            interest marker field). Never CC- or interest-specific here.
        :param bool allow_partial: when False (default), refuse to add
            a line to an invoice whose ``payment_state`` is
            ``'partial'``.
        :returns: the created ``account.move.line``.
        :raises UserError: on any guard failure (not POSTED out_invoice,
            hash-locked journal, locked fiscal period, or partial
            payment without ``allow_partial``).
        """
        self.ensure_one()

        # --- Guard 1: POSTED out_invoice only ---------------------------
        if self.move_type != 'out_invoice' or self.state != 'posted':
            raise UserError(_(
                "Cannot add a charge line to %(move)s: only a POSTED "
                "customer invoice accepts a charge line (move_type=%(type)s, "
                "state=%(state)s).",
                move=self.display_name,
                type=self.move_type,
                state=self.state,
            ))

        # --- Guard 2: hash-locked journal ---------------------------------
        if self.journal_id.restrict_mode_hash_table:
            raise UserError(_(
                "Cannot add a charge line to %(move)s: journal "
                "%(journal)s has the hash lock (restrict mode) enabled.",
                move=self.display_name,
                journal=self.journal_id.display_name,
            ))

        # --- Guard 3: locked fiscal period ---------------------------------
        # Core only runs this check automatically when 'date' or 'name'
        # changes (account_move.py write()); adding a line does not
        # trigger it, so it must be called explicitly here. Let core's
        # own UserError surface unchanged -- do not swallow it.
        self._check_fiscal_lock_dates()

        # --- Guard 4: partial payment ---------------------------------
        if not allow_partial and self.payment_state == 'partial':
            raise UserError(_(
                "Cannot add a charge line to %(move)s: invoice is "
                "partially paid (allow_partial=False).",
                move=self.display_name,
            ))

        # --- Snapshot reconciliation counterparts BEFORE writing ---------
        # Core unreconciles the receivable line when the move's
        # invoice_line_ids are rewritten on a posted entry (the
        # receivable balance changes to absorb the new line), so any
        # partial-payment matches must be redone afterwards.
        receivable_lines = self.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable'
        )
        reconcile_snapshot = []
        for line in receivable_lines:
            partials = line.matched_debit_ids | line.matched_credit_ids
            counterparts = self.env['account.move.line']
            for partial in partials:
                counterparts |= (
                    partial.debit_move_id
                    if partial.credit_move_id == line
                    else partial.credit_move_id
                )
            if counterparts:
                reconcile_snapshot.append((line, counterparts))

        # --- Write the line, via core's own documented escape hatch ------
        # account_move.py write() blocks invoice_line_ids/line_ids on a
        # posted move unless skip_readonly_check is set in context; core
        # itself uses this same key to bypass the guard (e.g. for bank
        # statement reconciliation writes). NEVER pass 'date' here.
        #
        # skip_is_manually_modified=True (adverse review): core's
        # write() force-sets is_manually_modified=True on ANY write
        # whose vals don't already include that key, unless this
        # context key is set (account_move.py write(), verified in the
        # real v19 checkout) -- core itself uses the same key when
        # POSTING an invoice for exactly this reason (see
        # action_post()/_post()'s own with_context(
        # skip_is_manually_modified=True) calls). Without it, every
        # invoice this cron ever touches gets permanently flagged as
        # hand-edited, which is wrong (it wasn't) and pollutes a field
        # core itself consults for vendor-bill auto-post heuristics.
        # Shared by both callers of this helper: the CC fee line
        # (lw_cc_surcharge) gets the same treatment for
        # free.
        # BUG FIX (adverse review -- found by RUNNING the code on a
        # staging instance, not by source review): a raw Command.create on
        # invoice_line_ids does NOT go through the onchange/wizard
        # flow that normally populates an invoice line's journal-side
        # fields. account.move.line._compute_balance's OWN branch for
        # invoice lines (account_move_line.py ~line 700, verified in
        # the real v19 checkout) is `else: line.balance = 0` --
        # unconditionally -- because core expects the real value to
        # already be present in the create vals for a normal invoice
        # line. Without it, the new line's price_subtotal, name, and
        # account are all correct, the line displays, price_subtotal
        # feeds amount_untaxed correctly, and the entry still "balances"
        # (0 vs 0) so nothing raises -- but balance/debit/credit/
        # amount_currency all stay 0, so amount_total/amount_residual
        # never move and the fee is a silent no-op. Confirmed live on
        # the pod: probe passed balance=-3.0 explicitly and got the
        # correct total=103.00; passing nothing gave total=100.00.
        #
        # Fix: compute and pass balance/amount_currency explicitly.
        # debit/credit do NOT need setting -- that compute
        # (_compute_debit_credit, @api.depends('balance')) is NOT
        # readonly=False and genuinely fires from whatever balance
        # ends up being, computed or provided; only balance and
        # amount_currency have the readonly=False escape hatch that
        # lets an explicit vals value pre-empt (rather than fight)
        # their own computes.
        #
        # Sign: direction_sign is core's OWN field for exactly this --
        # "Multiplicator depending on the document type, to convert a
        # price into a balance" (account_move.py). Not hardcoded to
        # -1: reading it off self.direction_sign means this helper
        # gets the correct sign for whatever move_type it is ever
        # called on, not just out_invoice (today's only caller, but
        # the signature makes no such promise). price_unit/
        # price_subtotal stay business-facing positive; only the
        # accounting-journal fields (balance, amount_currency, and by
        # extension debit/credit) carry the sign.
        #
        # FX: amount is denominated in THIS invoice's own currency
        # (self.currency_id), the same convention price_unit already
        # uses. amount_currency is the signed value in that currency;
        # balance is the signed value in COMPANY currency, converted
        # via the canonical res.currency._convert() (self or
        # to_currency safely no-ops the conversion when they're the
        # same currency -- verified in the real v19 res_currency.py --
        # so this is correct for both a same-currency invoice, which
        # is every existing caller today, and a foreign-currency one,
        # which nothing currently exercises but which this helper's
        # signature does not rule out).
        sign = self.direction_sign
        amount_currency_signed = float_round(
            sign * amount, precision_rounding=self.currency_id.rounding,
        )
        balance = self.currency_id._convert(
            amount_currency_signed,
            self.company_id.currency_id,
            self.company_id,
            self.invoice_date or self.date or fields.Date.context_today(self),
        )

        line_vals = {
            'product_id': product.id,
            'name': label,
            'quantity': 1,
            'price_unit': amount,
            'account_id': account.id,
            'tax_ids': [Command.clear()],
            'amount_currency': amount_currency_signed,
            'balance': balance,
        }
        line_vals.update(extra_line_vals or {})
        existing_line_ids = set(self.line_ids.ids)
        self.with_context(
            skip_readonly_check=True, skip_is_manually_modified=True,
        ).write({
            'invoice_line_ids': [Command.create(line_vals)],
        })

        new_line = self.line_ids.filtered(
            lambda l: l.id not in existing_line_ids and l.product_id == product
        )
        new_line.ensure_one()

        # --- Re-reconcile the counterparts core unmatched -----------------
        for line, counterparts in reconcile_snapshot:
            (line | counterparts).reconcile()

        # --- Clear the cached invoice PDF, if that module is installed ---
        if hasattr(self, '_lw_cc_clear_cached_invoice_pdf'):
            self._lw_cc_clear_cached_invoice_pdf()

        # --- Chatter marker -------------------------------------------------
        self.with_context(tracking_disable=True).message_post(
            body=_(
                "%(marker)s: charge line added -- %(label)s: %(amount)s "
                "(account %(account)s).",
                marker=CHARGE_LINE_CHATTER_MARKER,
                label=label,
                amount=f"{amount:.2f}",
                account=account.display_name,
            ),
            subtype_xmlid='mail.mt_note',
        )

        return new_line


# ------------------------------------------------------------------
# Interest as per-invoice lines
# ------------------------------------------------------------------


class AccountMoveServiceChargeFields(models.Model):
    """Per-invoice Charge Terms Interest tracking fields.

    Only meaningful when ``res.company.lw_cc_sc_mode == 'invoice_line'``.
    The month-key idempotency field is deliberately independent of
    ``invoice_date``: interest books into the invoice's ORIGINAL period
    by design (never rewritten -- see ``_lw_cc_add_charge_line``), so an
    old invoice's ``invoice_date`` cannot be used as a "was this invoice
    touched this month" signal the way the separate-invoice mode's
    ``ref``/date-range check can.
    """
    _inherit = 'account.move'

    lw_cc_sc_last_assessed_month = fields.Char(
        string="Interest Last Assessed (Month)",
        size=7,
        copy=False,
        tracking=True,
        help=(
            "YYYY-MM of the last month a Charge Terms Interest line was "
            "added to this invoice. Authoritative per-invoice "
            "idempotency key for lw_cc.service.charge.runner's "
            "'invoice_line' mode."
        ),
    )
    lw_cc_sc_total_assessed = fields.Monetary(
        string="Total Charge Terms Interest Assessed",
        currency_field='currency_id',
        copy=False,
        tracking=True,
        help=(
            "Running audit total of Charge Terms Interest lines added "
            "to this invoice across all months. The invoice lines "
            "themselves (marked via account.move.line."
            "lw_cc_sc_interest_line) remain the source of truth for "
            "amounts owed; this field is a convenience total only."
        ),
    )


class AccountMoveLineInterest(models.Model):
    """Authoritative marker for a Charge Terms Interest line.

    The product used for an interest line (``res.company.``
    ``lw_cc_surcharge_product_id``) is a display/accounting vehicle
    only and is shared with the legacy separate-invoice service charge
    -- never identify an interest line by product alone. This boolean is
    the one source of truth, consulted by the 'simple' compounding base
    calculation (below) and by the commission detail report's
    commission exclusion.
    """
    _inherit = 'account.move.line'

    lw_cc_sc_interest_line = fields.Boolean(
        string="Charge Terms Interest Line",
        default=False,
        copy=False,
        help=(
            "True when this line is a Charge Terms Interest assessment "
            "added by lw_cc.service.charge.runner in 'invoice_line' "
            "mode."
        ),
    )


class AccountMoveInterestLine(models.Model):
    """Adds one interest line to a posted past-due invoice.

    Thin wrapper around ``_lw_cc_add_charge_line``: resolves the
    product/account/label (same accounting chain as the legacy
    separate-invoice service charge) and stamps the authoritative
    ``lw_cc_sc_interest_line`` marker via ``extra_line_vals``. All
    guarding, reconciliation snapshot/restore, PDF cache clearing, and
    chatter are the shared helper's responsibility -- not reimplemented
    here.
    """
    _inherit = 'account.move'

    def _lw_cc_add_interest_line(self, amount, company, allow_partial):
        """
        :param float amount: interest amount for this invoice this month.
        :param company: ``res.company``.
        :param bool allow_partial: passed straight through to
            ``_lw_cc_add_charge_line`` -- caller decides per
            ``lw_cc_sc_partial_policy``.
        :returns: the created ``account.move.line``.
        :raises UserError: whatever ``_lw_cc_add_charge_line`` (or its
            own ``_check_fiscal_lock_dates()`` call) raises -- callers
            catch this to count skipped_locked/skipped_hashed/
            skipped_partial and move on to the next invoice.
        """
        self.ensure_one()

        product = (
            company.lw_cc_surcharge_product_id
            or self.env.ref(
                'lw_cc_surcharge.product_service_charge',
                raise_if_not_found=False,
            )
        )
        if not product:
            raise UserError(_(
                "Cannot add a Charge Terms Interest line to %(move)s: "
                "no service charge product configured for company "
                "%(company)s.",
                move=self.display_name,
                company=company.display_name,
            ))

        income_account = (
            company.lw_cc_surcharge_income_account_id
            or product.property_account_income_id
            or product.categ_id.property_account_income_categ_id
        )
        if not income_account:
            raise UserError(_(
                "Cannot add a Charge Terms Interest line to %(move)s: "
                "no income account available (neither configured nor "
                "product default) for company %(company)s.",
                move=self.display_name,
                company=company.display_name,
            ))

        label = _(
            "Monthly Charge Terms Interest (%(pct).2f%% per terms)",
            pct=company.lw_cc_surcharge_pct,
        )

        return self._lw_cc_add_charge_line(
            product, amount, income_account, label,
            extra_line_vals={'lw_cc_sc_interest_line': True},
            allow_partial=allow_partial,
        )

# ===================================================================
# Below: same-invoice CC fee line additions.
# ===================================================================
# -*- coding: utf-8 -*-
"""account.move extension: same-invoice CC fee line.

Owns only the CC-fee-specific product/account resolution and the
idempotency stamp on the triggering transaction; the actual posted-
invoice write (guards, reconciliation snapshot, PDF cache clear, chatter)
is delegated to the base module's generic
``_lw_cc_add_charge_line`` (lw_cc_surcharge/models/account_move.py).
"""
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class AccountMoveLineCCFee(models.Model):
    """Authoritative marker for a CC surcharge fee line.

    The product used for a CC fee
    line (``res.company.lw_cc_surcharge_product_id`` or the CC
    income account fallback chain) is a display/accounting vehicle
    only and is SHARED with the legacy service charge / interest
    product -- it can never be used to identify a fee line. Product-
    matching for this exact purpose is what produced the earlier failure elsewhere
    in this build (a card payment silently cancelling a customer's
    whole month of interest because the interest runner could not
    distinguish "interest line" from "any line on the SC/CC product").
    This boolean is the one source of truth, mirroring the base
    module's own ``lw_cc_sc_interest_line`` (lw_cc_surcharge/models/
    account_move.py's ``AccountMoveLineInterest``) -- the base module's
    simple-mode (non-compounding) interest base subtracts lines marked
    this way via ``getattr(line, 'lw_cc_fee_line', False)`` so it
    still works when this module is not installed.
    """
    _inherit = 'account.move.line'

    lw_cc_fee_line = fields.Boolean(
        string="CC Surcharge Fee Line",
        default=False,
        copy=False,
        help=(
            "True when this line is a credit card surcharge fee added "
            "by _lw_cc_add_cc_fee_line (same-invoice) or by "
            "the separate CCS fallback invoice's own single line. "
            "Never identify a CC fee line by product -- it shares the "
            "product with the Charge Terms Interest / service charge "
            "lines."
        ),
    )


class AccountMoveCCFeeLine(models.Model):
    _inherit = 'account.move'

    def _lw_cc_add_cc_fee_line(self, fee_amount, transaction):
        """Add the CC fee as a line on THIS posted invoice.

        Idempotent via ``transaction.lw_cc_surcharge_fee_line_id``: a
        second call for a transaction that already has a fee line
        returns it unchanged and writes nothing.

        Product / income-account resolution is IDENTICAL to the legacy
        engine's ``_create_cc_surcharge_invoice`` (company
        ``cc_income_account_id`` then the same fallback chain), so the
        fee books to the same account whether it lands here or, on
        fallback, a separate CCS invoice.

        :param float fee_amount: fee to add (already rounded by the
            caller -- ``_lw_cc_compute_portal_fee`` or the backend
            wizard).
        :param transaction: ``payment.transaction`` that triggered the
            fee; stamped with the new line on success.
        :returns: the new (or already-existing) ``account.move.line``.
        :raises UserError: propagated unchanged from
            ``_lw_cc_add_charge_line`` (not-posted, hash-locked journal,
            locked fiscal period, partial payment without allow), or
            raised here when no product/income account is configured.
        """
        self.ensure_one()
        transaction.ensure_one()
        if transaction.lw_cc_surcharge_fee_line_id:
            return transaction.lw_cc_surcharge_fee_line_id

        company = self.company_id
        product = (
            company.lw_cc_surcharge_product_id
            or self.env.ref(
                'lw_cc_surcharge.product_service_charge',
                raise_if_not_found=False,
            )
        )
        if not product:
            raise UserError(_(
                "Cannot add a CC fee line to %(move)s: no CC surcharge "
                "product configured for company %(company)s.",
                move=self.display_name, company=company.display_name,
            ))

        income_account = (
            company.lw_cc_surcharge_cc_income_account_id
            or company.lw_cc_surcharge_income_account_id
            or product.property_account_income_id
            or product.categ_id.property_account_income_categ_id
        )
        if not income_account:
            raise UserError(_(
                "Cannot add a CC fee line to %(move)s: no income "
                "account available for the CC surcharge product.",
                move=self.display_name,
            ))

        # The label must state the BASE the percentage was applied to,
        # not the charged total. Under the uplift transaction.amount is
        # base+fee (e.g. 103.00 on a $100 invoice at 3%): printing that
        # as "3.00% of 103.00" on the customer's own posted invoice
        # contradicts itself (3% of 103 is 3.09, not the 3.00 actually
        # charged). Recovering the base by subtracting fee_amount back
        # out is exact -- it is the same arithmetic the caller used to
        # build transaction.amount in the first place (base + fee).
        base_amount = transaction.amount - fee_amount
        new_line = self._lw_cc_add_charge_line(
            product=product,
            amount=fee_amount,
            account=income_account,
            label=_(
                "Credit Card Processing Fee (%(pct).2f%% of %(amount)s)",
                pct=company.lw_cc_surcharge_cc_pct,
                amount=f"{base_amount:.2f}",
            ),
            # allow_partial=True: this line is being added IN RESPONSE
            # TO the very payment that is about to settle it (or, for an
            # overdue batch, the oldest of several invoices the same
            # payment covers) -- refusing on payment_state == 'partial'
            # here would defeat the design for exactly the invoices it
            # exists to help, silently pushing every one of them onto
            # the separate-invoice fallback instead.
            allow_partial=True,
            # The authoritative marker -- never let a caller
            # (this module's own runner branches, lw_cc_surcharge's
            # interest base, the commission detail report) infer "is
            # this a CC fee line" from the product, which is shared
            # with the interest/service-charge lines.
            extra_line_vals={'lw_cc_fee_line': True},
        )
        transaction.lw_cc_surcharge_fee_line_id = new_line.id
        return new_line
