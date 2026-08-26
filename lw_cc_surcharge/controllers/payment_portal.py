# -*- coding: utf-8 -*-
"""Portal payment controller for checkout-time CC surcharge (quote + uplift).

Extends the account_payment invoice transaction flow:

- ``/lw_cc_surcharge/quote`` (NEW, jsonrpc, auth public): given a card
  BIN (digits-sanitized first-6) or a saved token, answers whether the
  credit card fee applies and with which amounts. The verdict is stored in
  the session only (BIN + card type, TTL 1h, consumed at tx creation).
- ``_process_transaction`` override: when every gate passes, the
  server-validated base amount is uplifted by the fee so ONE transaction
  charges invoice + fee. The client amount only SELECTS a menu amount; it
  is never trusted (tampered or absent amounts fall back to the full
  residual).
- ``_create_transaction`` override: merges the fee onto the transaction
  create values via custom_create_values, preserving the invoice_ids
  Command the base class injects.

Fail-safe posture (deliberately opposite of the legacy engine): DEBIT and
unknown cards are NEVER surcharged in this path, and EPD invoices are
never uplifted. With the company flag off, both overrides are
byte-identical passthroughs.
"""
import logging
import re
import time

from odoo import _, fields, http
from odoo.exceptions import AccessError, MissingError
from odoo.http import request

from odoo.addons.account_payment.controllers.payment import PaymentPortal

_logger = logging.getLogger(__name__)

# Session verdict lifetime: a quote older than this is not honored at
# transaction creation (the customer re-enters/re-selects the card).
_LW_CC_QUOTE_TTL = 3600  # seconds

CC_WAIVE_CHATTER_MARKER = 'lw_cc_surcharge:fee_waived'


class LwCcSurchargePaymentPortal(PaymentPortal):

    # ==================================================================
    # Route 1: quote
    # ==================================================================

    @http.route(
        '/lw_cc_surcharge/quote',
        type='jsonrpc', auth='public', website=False,
    )
    def lw_cc_quote(
        self, invoice_id=0, access_token=None, card_bin=None,
        token_id=None, overdue=False, **kw,
    ):
        """Quote the portal CC surcharge for a card BIN or a saved token.

        Failure responses leak nothing beyond a coarse reason: access
        errors, gate misses and unknown cards are indistinguishable from
        the outside.

        :param int invoice_id: The invoice being paid (ignored with
                               ``overdue``).
        :param str access_token: Portal access token of the invoice.
        :param str card_bin: Card digits; only the first 6 are ever used.
        :param int token_id: Saved payment token: read the verdict from
                             the token instead of a raw BIN.
        :param bool overdue: Quote the logged-in partner's overdue-invoice
                             batch instead of a single invoice.
        :return: ``{'applies': True, 'fee', 'base', 'total', 'pct'}`` or
                 ``{'applies': False, 'reason'}`` with reason in
                 access|bin|gates|epd|debit|unknown.
        :rtype: dict
        """
        if overdue:
            return self._lw_cc_quote_overdue(
                card_bin=card_bin, token_id=token_id,
            )

        # --- document access: never confirm or deny beyond the reason ---
        try:
            invoice_id = int(invoice_id or 0)
        except (TypeError, ValueError):
            invoice_id = 0
        if not invoice_id:
            return {'applies': False, 'reason': 'access'}
        try:
            invoice_sudo = self._document_check_access(
                'account.move', invoice_id, access_token,
            )
        except (AccessError, MissingError):
            return {'applies': False, 'reason': 'access'}

        # --- config + document gates (mirror _assess_cc_surcharge) ------
        gates_fail = self._lw_cc_quote_gates_fail(invoice_sudo)
        if gates_fail:
            return {'applies': False, 'reason': gates_fail}

        # --- card verdict: saved token first, else sanitized BIN --------
        reason, card_type, bin_prefix = self._lw_cc_quote_card_verdict(
            card_bin, token_id, invoice_sudo.commercial_partner_id,
        )
        if reason:
            return {'applies': False, 'reason': reason}

        # --- base: same menu validation as the uplift -------------------
        base = self._lw_cc_portal_base_amount(invoice_sudo, kw.get('amount'))
        if base is None or base <= 0.0:
            return {'applies': False, 'reason': 'gates'}

        company = invoice_sudo.company_id
        cc_pct = company.lw_cc_surcharge_cc_pct or 0.0
        fee = request.env['payment.transaction']._lw_cc_compute_portal_fee(
            base, company, currency=invoice_sudo.currency_id,
        )
        if fee <= 0.0:
            return {'applies': False, 'reason': 'gates'}

        # Verdict for the uplift: 6-digit BIN + card type only, TTL is
        # enforced at READ time (transaction creation).
        session_key = 'lw_cc_quote_%s_%s' % (
            invoice_sudo.commercial_partner_id.id, invoice_sudo.id,
        )
        request.session[session_key] = {
            'bin': bin_prefix,
            'card_type': 'CREDIT',
            'ts': time.time(),
        }
        return {
            'applies': True,
            'fee': fee,
            'base': base,
            'total': base + fee,
            'pct': cc_pct,
            'currency_symbol': company.currency_id.symbol,
            'currency_position': company.currency_id.position,
        }

    def _lw_cc_quote_overdue(self, card_bin=None, token_id=None):
        """Quote the fee for the logged-in partner's overdue batch.

        Overdue payment is logged-in only (core route raises on public
        users); the base is the sum of the batch residuals because the
        overdue page has no installment menu.
        """
        if request.env.user._is_public():
            return {'applies': False, 'reason': 'access'}

        partner = request.env.user.partner_id
        # Same set the core overdue route derives server-side, filtered to
        # the posted customer invoices the uplift can actually settle.
        overdue_invoices = request.env['account.move'].sudo().search(
            self._lw_cc_get_overdue_invoices_domain()
        ).filtered(
            lambda inv: inv.move_type == 'out_invoice' and inv.state == 'posted'
        )
        if not overdue_invoices:
            return {'applies': False, 'reason': 'gates'}

        company = overdue_invoices[0].company_id
        cc_pct = company.lw_cc_surcharge_cc_pct or 0.0
        applicable_terms = company.lw_cc_surcharge_applicable_term_ids
        if (
            not company.lw_cc_surcharge_portal_uplift
            or not company.lw_cc_surcharge_enabled
            or cc_pct <= 0.0
            or not applicable_terms
            or partner.commercial_partner_id.lw_cc_surcharge_optout
            # Fail-safe: one non-eligible term in the batch blocks the
            # whole uplift (never surcharge a Net 0 portion).
            or any(
                inv.invoice_payment_term_id not in applicable_terms
                for inv in overdue_invoices
            )
            # Dry-run: never quote a fee the checkout will not charge
            # (same rationale as _lw_cc_quote_gates_fail above).
            or company._lw_cc_fee_is_dry_run()
        ):
            return {'applies': False, 'reason': 'gates'}

        reason, card_type, bin_prefix = self._lw_cc_quote_card_verdict(
            card_bin, token_id, partner.commercial_partner_id,
        )
        if reason:
            return {'applies': False, 'reason': reason}

        base = sum(overdue_invoices.mapped('amount_residual'))
        if base <= 0.0:
            return {'applies': False, 'reason': 'gates'}

        # DELIBERATE GAP (currency investigation -- do not
        # "fix" this without re-reading the reasoning below):
        #
        # This quote route has NO currency-uniformity gate of its own.
        # It does not verify all overdue_invoices share ONE currency,
        # and it computes the fee here using overdue_invoices[0]
        # .currency_id -- the same "first invoice's currency" shortcut
        # `company` already takes two lines above. A currency-mismatched
        # overdue batch can therefore still QUOTE a fee.
        #
        # The ACTUAL charge path (_lw_cc_uplift_transaction_kwargs
        # below) DOES have the gate: it checks currency_id ==
        # company.currency_id for every invoice in the batch before
        # ever uplifting. So the failure mode here is display-only --
        # a quoted fee that then never gets charged -- never the
        # reverse (a charged fee that was never shown), which is the
        # direction that would create a disclosure problem on a
        # surcharge feature. No money moves incorrectly.
        #
        # Left as-is on purpose: repo evidence says this business is
        # single-currency USD-domestic (see the currency investigation
        # report), so this is latent in the same way the whole currency
        # question is latent, and a fix here is scope the adverse
        # review did not ask for and could not be justified as
        # necessary. If a merchant ever invoices in multiple currencies,
        # mirror the same per-invoice currency check
        # _lw_cc_uplift_transaction_kwargs uses, into this method's own
        # gate chain above (near the opt-out/term checks).
        fee = request.env['payment.transaction']._lw_cc_compute_portal_fee(
            base, company, currency=overdue_invoices[0].currency_id,
        )
        if fee <= 0.0:
            return {'applies': False, 'reason': 'gates'}

        session_key = 'lw_cc_quote_%s_overdue' % (
            partner.commercial_partner_id.id,
        )
        request.session[session_key] = {
            'bin': bin_prefix,
            'card_type': 'CREDIT',
            'ts': time.time(),
        }
        return {
            'applies': True,
            'fee': fee,
            'base': base,
            'total': base + fee,
            'pct': cc_pct,
            'currency_symbol': company.currency_id.symbol,
            'currency_position': company.currency_id.position,
        }

    def _lw_cc_quote_gates_fail(self, invoice_sudo):
        """Evaluate the quote gates on one invoice.

        :return: The failure reason ('gates' or 'epd'), or None when the
                 invoice is upliftable.
        :rtype: str|None
        """
        company = invoice_sudo.company_id
        cc_pct = company.lw_cc_surcharge_cc_pct or 0.0
        applicable_terms = company.lw_cc_surcharge_applicable_term_ids
        if (
            not company.lw_cc_surcharge_portal_uplift
            or not company.lw_cc_surcharge_enabled
            or cc_pct <= 0.0
            or not applicable_terms
            or invoice_sudo.invoice_payment_term_id not in applicable_terms
            or invoice_sudo.commercial_partner_id.lw_cc_surcharge_optout
            # Currency-mismatch gate (currency investigation, closed
            # _lw_cc_compute_portal_fee rounds by the
            # currency it is given -- an invoice in a currency other
            # than the company's must never quote or charge the fee.
            # Mirrors the backend wizard's own currency_id ==
            # company.currency_id gate
            # (wizards/account_payment_register.py's
            # _lw_cc_surcharge_gates_pass).
            or invoice_sudo.currency_id != company.currency_id
        ):
            return 'gates'

        # Dry-run gate. Quoting a fee the checkout will not charge would
        # show the customer a total that does not match what is taken
        # from their card, so dry-run has to suppress the quote as well
        # as the uplift. Returns the existing 'gates' reason rather than
        # a new one: the client-side handler treats every non-'applies'
        # reason the same way, and a new string would be a change to
        # that contract for no gain.
        if company._lw_cc_fee_is_dry_run():
            return 'gates'

        if (
            invoice_sudo.move_type != 'out_invoice'
            or invoice_sudo.state != 'posted'
            or invoice_sudo.currency_id.is_zero(invoice_sudo.amount_residual)
        ):
            return 'gates'

        # The base _create_payment gates the early-payment-discount
        # write-off on an EXACT amount match; an uplifted amount would
        # silently skip it. EPD invoices are never uplifted.
        next_values = invoice_sudo._get_invoice_next_payment_values()
        if next_values.get('installment_state') == 'epd':
            return 'epd'

        return None

    def _lw_cc_quote_card_verdict(self, card_bin, token_id, commercial_partner):
        """Resolve the card type behind a quote request.

        Saved token first (verdict field written by _post_process), raw
        BIN second (digits-sanitized first-6, looked up in
        lw_cc.bin.record). Unknown -- no BIN record, a token without a
        verdict, or a token belonging to another partner -- fails CLOSED:
        the legacy engine surcharged unknown cards, this path never does.

        :param commercial_partner: ``res.partner`` the card must belong to.
        :return: tuple (reason, card_type, bin_prefix); reason is None
                 only when card_type == 'CREDIT', else the reason the
                 caller must return. bin_prefix is '' for token quotes.
        """
        bin_prefix = ''
        if token_id:
            try:
                token_id = int(token_id)
            except (TypeError, ValueError):
                return 'unknown', None, bin_prefix
            token_sudo = request.env['payment.token'].sudo().browse(
                token_id,
            ).exists()
            if not token_sudo or (
                token_sudo.partner_id.commercial_partner_id != commercial_partner
            ):
                # Foreign or absent token: answer 'unknown', never
                # confirming or denying foreign token ids.
                return 'unknown', None, bin_prefix
            card_type = token_sudo.lw_cc_surcharge_bin_check or None
        else:
            bin_prefix = re.sub(r'\D', '', str(card_bin or ''))[:6]
            if len(bin_prefix) < 6:
                return 'bin', None, bin_prefix
            record = request.env['lw_cc.bin.record'].sudo().lookup_bin(
                bin_prefix,
            )
            card_type = record.card_type if record else None

        if not card_type:
            return 'unknown', None, bin_prefix
        if card_type != 'CREDIT':
            return 'debit', None, bin_prefix
        return None, card_type, bin_prefix

    # ==================================================================
    # Route 2 path: uplift the transaction amount
    # ==================================================================

    @http.route()
    def invoice_transaction(self, invoice_id, access_token, **kwargs):
        """Pop the impersonated-waive flag before the core kwargs whitelist.

        ``_validate_transaction_kwargs`` (payment/controllers/portal.py)
        400s on any kwarg outside its whitelist, and ``lw_cc_waive`` is
        not on it. Popping here is purely mechanical bookkeeping -- it is
        NOT authorization; the actual re-authorization against the
        impersonation session happens downstream in
        ``_lw_cc_uplift_transaction_kwargs`` ("buttons hide, they don't
        authorize").
        """
        if kwargs.pop('lw_cc_waive', None):
            request.update_context(lw_cc_waive=True)
        return super().invoice_transaction(invoice_id, access_token, **kwargs)

    @http.route()
    def overdue_invoices_transaction(self, payment_reference, **kwargs):
        """Pop the impersonated-waive flag before the core kwargs whitelist.

        Same rationale as ``invoice_transaction`` above -- the overdue
        batch route runs through the identical whitelist check.
        """
        if kwargs.pop('lw_cc_waive', None):
            request.update_context(lw_cc_waive=True)
        return super().overdue_invoices_transaction(payment_reference, **kwargs)

    def _process_transaction(
        self, partner_id, currency_id, invoice_ids, payment_reference, **kwargs,
    ):
        """Uplift the charged amount with the quoted CC fee when armed.

        Only the invoice transaction routes reach here (website checkout
        calls ``_create_transaction`` directly and is untouched). When any
        gate fails, kwargs pass through unchanged -- flag-off means a
        byte-identical legacy path.
        """
        kwargs = self._lw_cc_uplift_transaction_kwargs(
            partner_id, currency_id, invoice_ids, kwargs,
        )
        return super()._process_transaction(
            partner_id, currency_id, invoice_ids, payment_reference, **kwargs,
        )

    def _lw_cc_uplift_transaction_kwargs(
        self, partner_id, currency_id, invoice_ids, kwargs,
    ):
        """Return kwargs with the amount uplifted by the fee, or unchanged.

        Gates are evaluated cheapest-first; the FIRST failure returns the
        kwargs untouched and sets no context keys, so the
        ``_create_transaction`` override passthrough matches exactly.
        """
        if not invoice_ids:
            return kwargs
        invoices = request.env['account.move'].sudo().browse(invoice_ids).exists()\
            .filtered(lambda inv: inv.move_type == 'out_invoice' and inv.state == 'posted')
        if not invoices:
            return kwargs

        # --- config gates (company of the first invoice) -----------------
        company = invoices[0].company_id
        cc_pct = company.lw_cc_surcharge_cc_pct or 0.0
        applicable_terms = company.lw_cc_surcharge_applicable_term_ids
        if (
            not company.lw_cc_surcharge_portal_uplift
            or not company.lw_cc_surcharge_enabled
            or cc_pct <= 0.0
            or not applicable_terms
            # Fail-safe: any invoice's commercial partner opted out of
            # the CC surcharge blocks the whole uplift.
            or any(
                inv.commercial_partner_id.lw_cc_surcharge_optout
                for inv in invoices
            )
            or any(
                inv.invoice_payment_term_id not in applicable_terms
                for inv in invoices
            )
            # Currency-mismatch gate, per-invoice across the batch
            # (same rationale as _lw_cc_quote_gates_fail above): an
            # overdue-batch uplift with even ONE foreign-currency
            # invoice in it must never uplift the whole batch.
            or any(
                inv.currency_id != company.currency_id for inv in invoices
            )
        ):
            return kwargs

        # --- dry-run gate -----------------------------------------------
        # THE most important gate in this method. Everything below it
        # decides what the customer's card is actually charged: the
        # returned kwargs carry an amount inflated by the fee, and the
        # card is charged that amount before any invoice line exists.
        # Dry-run therefore has to stop the fee HERE, where it is
        # decided, and not at settlement (_lw_cc_apply_cc_fee): by
        # settlement time the money has already left the customer's
        # card, and suppressing the invoice line then would take real
        # money and record nothing -- strictly worse than charging it.
        #
        # Returning kwargs untouched is exactly the "no uplift" path
        # every other gate above takes, so lw_cc_surcharge_fee_amount
        # is never set, and _create_payment / _post_process /
        # _lw_cc_apply_cc_fee all no-op on their own existing
        # `if not fee` guards. Nothing downstream needs to know.
        if company._lw_cc_fee_is_dry_run():
            _logger.info(
                "lw_cc_surcharge [DRY-RUN]: not uplifting "
                "the portal payment for invoice(s) %s (%.2f%% CC fee "
                "suppressed; the customer is charged the invoice amount "
                "only). Untick Dry-Run Mode in Settings to charge it.",
                ", ".join(invoices.mapped('name')), cc_pct,
            )
            return kwargs

        # --- card-only gate, resolved to the PRIMARY method like the
        # base engine (brand methods resolve back to 'card').
        flow = kwargs.get('flow')
        token_sudo = None
        if flow == 'token':
            token_sudo = request.env['payment.token'].sudo().browse(
                kwargs.get('token_id') or 0,
            ).exists()
            if not token_sudo:
                return kwargs
            payment_method = token_sudo.payment_method_id
        else:
            payment_method = request.env['payment.method'].sudo().browse(
                kwargs.get('payment_method_id') or 0,
            ).exists()
        if not payment_method:
            return kwargs
        primary_method = (
            payment_method.primary_payment_method_id or payment_method
        )
        if primary_method.code != 'card':
            return kwargs

        # --- base: overdue batch (multiple invoices) vs single invoice ---
        if len(invoices) > 1:
            # The overdue route derives the ids server-side and the page
            # has no installment menu: base is the sum of residuals.
            base = sum(invoices.mapped('amount_residual'))
            session_key = 'lw_cc_quote_%s_overdue' % (
                invoices[0].commercial_partner_id.id,
            )
        else:
            invoice = invoices[0]
            if invoice._get_invoice_next_payment_values()\
                    .get('installment_state') == 'epd':
                return kwargs  # EPD write-off assumes the exact amount_due
            base = self._lw_cc_portal_base_amount(
                invoice, kwargs.get('amount'),
            )
            session_key = 'lw_cc_quote_%s_%s' % (
                invoice.commercial_partner_id.id, invoice.id,
            )
        if base is None or base <= 0.0:
            return kwargs

        # --- verdict: token field, or the consumed session quote ---------
        if flow == 'token':
            if (token_sudo.lw_cc_surcharge_bin_check or None) != 'CREDIT':
                return kwargs
        else:
            verdict = request.session.pop(session_key, None)  # consumed on use
            if not verdict or time.time() - verdict.get('ts', 0) > _LW_CC_QUOTE_TTL:
                return kwargs
            record = request.env['lw_cc.bin.record'].sudo().lookup_bin(
                verdict.get('bin') or '',
            )
            # Trust the stored card_type only if the BIN still resolves to
            # a credit card TODAY (ranges shift as banks reissue cards).
            if not record or record.card_type != 'CREDIT':
                return kwargs

        # invoices[0].currency_id == company.currency_id is now
        # guaranteed by the currency-mismatch gate above (every invoice
        # in the batch was checked), so this is the correct rounding
        # currency for both the single-invoice and overdue-batch cases.
        fee = request.env['payment.transaction']._lw_cc_compute_portal_fee(
            base, company, currency=invoices[0].currency_id,
        )
        if fee <= 0.0:
            return kwargs

        # --- staff waive (impersonated portal session only) --------------
        # The checkbox in the template is presentation only ("buttons
        # hide, they don't authorize" -- the impersonation module's
        # res_partner.py precedent). Re-authorize here against
        # impersonate_from_uid, the session key OCA impersonate_login()
        # sets ONLY for a staff member's Switch Login -- a real customer's
        # own session can never carry it (see the design notes finding A). A
        # forged flag with no impersonation session is IGNORED (warning
        # logged, fee still applies) rather than honored.
        if request.env.context.get('lw_cc_waive'):
            impersonate_from_uid = request.session.get('impersonate_from_uid')
            if impersonate_from_uid:
                staff = request.env['res.users'].sudo().browse(
                    impersonate_from_uid,
                )
                # message_post requires a singleton (mail.thread's
                # ensure_one()); `invoices` is multi-record on the
                # overdue-batch path, so post PER INVOICE rather than
                # on the recordset as a whole -- a batch of N invoices
                # gets N chatter entries, each cross-referencing the
                # full batch by name, so every invoice stays
                # individually traceable (the whole point of an audit
                # note). Single-invoice waives keep the exact same
                # message text as before this fix.
                if len(invoices) > 1:
                    waive_body = _(
                        "%(marker)s: credit card fee of %(fee)s waived "
                        "by %(staff)s during an impersonated portal "
                        "payment covering this invoice's overdue batch "
                        "(%(names)s).",
                        marker=CC_WAIVE_CHATTER_MARKER,
                        fee=f"{fee:.2f}",
                        staff=staff.display_name,
                        names=", ".join(invoices.mapped('name')),
                    )
                else:
                    waive_body = _(
                        "%(marker)s: credit card fee of %(fee)s waived by "
                        "%(staff)s during an impersonated portal payment.",
                        marker=CC_WAIVE_CHATTER_MARKER,
                        fee=f"{fee:.2f}",
                        staff=staff.display_name,
                    )
                for inv in invoices:
                    inv.sudo().with_context(tracking_disable=True).message_post(
                        body=waive_body,
                        subtype_xmlid='mail.mt_note',
                    )
                _logger.info(
                    "lw_cc_surcharge: fee %.2f waived by "
                    "staff uid %s (impersonated) for invoice_ids %s.",
                    fee, impersonate_from_uid, invoice_ids,
                )
                return kwargs  # unchanged: base amount only, no uplift
            _logger.warning(
                "lw_cc_surcharge: lw_cc_waive flag "
                "present with no impersonation session (invoice_ids %s) "
                "-- ignoring; the fee still applies.",
                invoice_ids,
            )

        kwargs = dict(kwargs)
        kwargs['amount'] = base + fee
        # The base _process_transaction builds its own custom_create_values
        # (the invoice_ids Command); stash the fee on the request context
        # for the _create_transaction override below to merge.
        request.update_context(
            lw_cc_uplift_fee=fee,
            lw_cc_uplift_bin='CREDIT',
        )
        _logger.info(
            "lw_cc_surcharge: uplifting invoice_ids %s: "
            "base %.2f + fee %.2f = %.2f (pct %.2f, %s flow).",
            invoice_ids, base, fee, base + fee, cc_pct,
            'token' if flow == 'token' else 'card entry',
        )
        return kwargs

    def _create_transaction(self, *args, **kwargs):
        """Merge the uplift fee onto the transaction create values.

        Runs after the base ``_process_transaction`` built its own
        custom_create_values (the invoice_ids Command): merging into that
        dict preserves the Command while adding the fee fields. No-op when
        the uplift context keys are absent -- website checkout and every
        other caller get a byte-identical call.
        """
        fee = request.env.context.get('lw_cc_uplift_fee')
        if fee:
            custom = dict(kwargs.get('custom_create_values') or {})
            custom['lw_cc_surcharge_fee_amount'] = fee
            custom['lw_cc_surcharge_bin_check'] = (
                request.env.context.get('lw_cc_uplift_bin')
            )
            kwargs['custom_create_values'] = custom
        return super()._create_transaction(*args, **kwargs)

    # ==================================================================
    # Shared helpers
    # ==================================================================

    def _get_extra_payment_form_values(self, **kwargs):
        """Expose the arming flag so the fee row template can gate on it.

        The flag-off payment form must render NOTHING extra: the template
        row t-ifs on this value, and the JS is inert when the row is
        absent. On payment-link flows the invoice's company decides
        (``invoice_id`` kwarg); on the pay modal the current company
        (single-company in practice).
        """
        form_values = super()._get_extra_payment_form_values(**kwargs)
        company = request.env.company
        invoice_id = self._cast_as_int(kwargs.get('invoice_id'))
        if invoice_id:
            invoice = request.env['account.move'].sudo().browse(
                invoice_id,
            ).exists()
            if invoice:
                company = invoice.company_id
        form_values['lw_cc_surcharge_portal_uplift'] = bool(
            company.lw_cc_surcharge_portal_uplift
        )
        return form_values

    def _lw_cc_portal_base_amount(self, invoice, client_amount):
        """Validate the client amount against the portal payment menu.

        The modal only ever submits one of the amounts the menu renders
        (the next installment, the full residual, or any single open
        installment residual). A client amount outside that set is tampered
        or stale: fall back to the FULL residual (conservative -- never
        undercharges the base).

        :param account.move invoice: Sudoed posted invoice.
        :param float|None client_amount: Amount submitted by the client.
        :return: The validated base, or None for EPD invoices / invoices
                 without payment term lines (callers skip the uplift).
        :rtype: float|None
        """
        next_values = invoice._get_invoice_next_payment_values()
        if not next_values or next_values.get('installment_state') == 'epd':
            return None
        allowed = {
            round(next_values['next_amount_to_pay'], 2),
            round(next_values['amount_due'], 2),
            round(invoice.amount_residual, 2),
        }
        allowed |= {
            round(inst['amount_residual_currency_unsigned'], 2)
            for inst in next_values.get('not_reconciled_installments', [])
        }
        if client_amount is not None:
            try:
                if round(float(client_amount), 2) in allowed:
                    return float(client_amount)
            except (TypeError, ValueError):
                pass
        return invoice.amount_residual

    def _lw_cc_get_overdue_invoices_domain(self, partner_id=None):
        """Overdue invoice domain, replicating account's PortalAccount one.

        ``_get_overdue_invoices_domain`` is defined on
        ``account.controllers.portal.PortalAccount``, which is not in this
        controller's inheritance chain (account_payment's PaymentPortal
        extends payment's CustomerPortal), so the v19 domain is replicated
        here verbatim.
        """
        return [
            ('state', 'not in', ('cancel', 'draft')),
            ('move_type', 'in', ('out_invoice', 'out_receipt')),
            ('payment_state', 'not in', (
                'in_payment', 'paid', 'reversed', 'blocked', 'invoicing_legacy',
            )),
            ('invoice_date_due', '<', fields.Date.today()),
            ('partner_id', '=', partner_id or request.env.user.partner_id.id),
        ]
