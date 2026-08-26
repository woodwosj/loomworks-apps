/** @odoo-module **/
/*
    CC surcharge fee disclosure on the portal invoice payment form.

    Patches the PaymentForm interaction so that, on invoice flows
    (/invoice/transaction/<id> and /invoice/transaction/overdue), the customer
    sees the credit card fee before paying:

    - a saved card (token) is quoted as soon as it is selected (token_id param)
    - a new card shows a plain disclosure line, then the fee and new total once
      the first 6 digits of the card number are typed (card_bin param)
    - submit re-quotes best-effort so the displayed fee and the server-side
      session verdict match the final card number

    Every rpc and DOM access fails open: on any error the payment proceeds and
    the row falls back to the plain disclosure line. No OWL state, pure DOM.
    The server (/lw_cc_surcharge/quote + the _process_transaction uplift
    override) remains authoritative for whether a fee applies and for how much.

    See the module build notes.
*/

import { rpc } from "@web/core/network/rpc";
import { patch } from "@web/core/utils/patch";

import { PaymentForm } from "@payment/interactions/payment_form";

const LW_CC_QUOTE_ROUTE = "/lw_cc_surcharge/quote";
const LW_CC_INVOICE_ROUTE_MARKER = "/invoice/transaction/";
const LW_CC_DISCLOSURE_PENDING =
    "A credit card fee may apply to this payment. The exact amount shows once you enter your card number.";

patch(PaymentForm.prototype, {

    // #=== OVERRIDES ===#

    /**
     * Quote the fee for the payment option that is pre-selected on load.
     *
     * The radio change handler below only fires on user interaction, but the
     * form opens with the first saved card pre-selected: without this hook the
     * customer would pay the fee without ever seeing it.
     *
     * @override method from payment.payment_form
     * @return {void}
     */
    async willStart() {
        await super.willStart(...arguments);
        if (!this._lw_ccIsInvoiceFlow()) {
            return;
        }
        // Absent from the DOM on a real customer's page (server t-if); a
        // no-op query on their page, live only for an impersonated session.
        this._lw_ccBindWaiveCheckbox();
        try {
            const checkedRadio = document.querySelector('input[name="o_payment_radio"]:checked');
            if (checkedRadio) {
                await this._lw_ccDispatchSelection(checkedRadio);
            }
        } catch (error) {
            console.warn("[lw_cc_surcharge] willStart quote failed", error);
            this._lw_ccShowDisclosure();
        }
    },

    /**
     * Update the fee row when the customer selects another payment option.
     *
     * @override method from payment.payment_form
     * @param {Event} ev
     * @return {void}
     */
    async selectPaymentOption(ev) {
        await super.selectPaymentOption(...arguments);
        if (!this._lw_ccIsInvoiceFlow()) {
            return;
        }
        try {
            await this._lw_ccDispatchSelection(ev.target);
        } catch (error) {
            console.warn("[lw_cc_surcharge] selectPaymentOption quote failed", error);
            this._lw_ccShowDisclosure();
        }
    },

    /**
     * Listen for card number entry in the Authorize.net inline form.
     *
     * Runs after the whole super chain (the Authorize.net patch included, this
     * module loads after payment_authorize), so the inline form is prepared.
     *
     * @override method from payment.payment_form
     * @param {number} providerId - The id of the selected payment option's provider.
     * @param {string} providerCode - The code of the selected payment option's provider.
     * @param {number} paymentOptionId - The id of the selected payment option.
     * @param {string} paymentMethodCode - The code of the selected payment method, if any.
     * @param {string} flow - The online payment flow of the selected payment option.
     * @return {void}
     */
    async _prepareInlineForm(providerId, providerCode, paymentOptionId, paymentMethodCode, flow) {
        await super._prepareInlineForm(...arguments);
        if (providerCode !== "authorize" || paymentMethodCode !== "card" || flow === "token") {
            return;
        }
        this._lw_ccBindCardInput();
    },

    /**
     * Re-quote the fee before the transaction is created.
     *
     * Best-effort only: any failure is swallowed and the payment proceeds. On
     * success this both refreshes the displayed amounts and re-stores the
     * server-side session verdict with the final card number, which the uplift
     * override consumes when creating the transaction.
     *
     * @override method from payment.payment_form
     * @param {Event} ev
     * @return {void}
     */
    async submitForm(ev) {
        if (this._lw_ccIsInvoiceFlow()) {
            try {
                const checkedRadio = this.el.querySelector(
                    'input[name="o_payment_radio"]:checked'
                );
                const optionType = checkedRadio ? this._getPaymentOptionType(checkedRadio) : null;
                const providerCode = checkedRadio ? this._getProviderCode(checkedRadio) : null;
                const paymentMethodCode = checkedRadio
                    ? this._getPaymentMethodCode(checkedRadio) : null;
                const isCardFlow = optionType !== "token"
                    && providerCode === "authorize"
                    && paymentMethodCode === "card";
                if (isCardFlow) {
                    const digits = this._lw_ccCardDigits();
                    if (digits.length >= 6) {
                        const seq = this._lw_ccNextSeq();
                        const quote = await this._lw_ccQuote(digits, null);
                        if (seq === this._lw_ccQuoteSeq) {
                            this._lw_ccApplyQuote(quote);
                        }
                    }
                }
            } catch (error) {
                console.warn("[lw_cc_surcharge] submit re-quote failed", error);
            }
        }
        await super.submitForm(...arguments);
    },

    /**
     * Post the staff waive flag when the box is checked.
     *
     * The row/checkbox are absent from the DOM entirely for a real
     * customer, so this reads as always-unchecked (falsy) on their page
     * and the param is simply never added -- byte-identical to today.
     * The server independently re-authorizes the waive against the
     * impersonation session; this only carries the staff's request.
     *
     * @override method from payment.payment_form
     * @private
     * @return {object} The transaction route params.
     */
    _prepareTransactionRouteParams() {
        const transactionRouteParams = super._prepareTransactionRouteParams(...arguments);
        const waiveCheckbox = document.getElementById("lw_cc_surcharge_waive_checkbox");
        if (this._lw_ccIsInvoiceFlow() && waiveCheckbox && waiveCheckbox.checked) {
            transactionRouteParams.lw_cc_waive = true;
        }
        return transactionRouteParams;
    },

    // #=== FEE ROW DISPATCH ===#

    /**
     * Drive the fee row for the given selected radio button.
     *
     * Token radios quote immediately (the server resolves the card type from
     * the token). Card method radios show the plain disclosure until enough of
     * the card number is typed. Any other payment option hides the row.
     *
     * @private
     * @param {HTMLInputElement} radio - The radio button linked to the payment option.
     * @return {void}
     */
    async _lw_ccDispatchSelection(radio) {
        if (!radio) {
            this._lw_ccHideRow();
            return;
        }
        if (this._getPaymentOptionType(radio) === "token") {
            const seq = this._lw_ccNextSeq();
            const quote = await this._lw_ccQuote("", this._getPaymentOptionId(radio));
            if (seq !== this._lw_ccQuoteSeq) {
                return; // A newer selection already took over the row.
            }
            this._lw_ccApplyQuote(quote);
            return;
        }
        const providerCode = this._getProviderCode(radio);
        const paymentMethodCode = this._getPaymentMethodCode(radio);
        if (providerCode === "authorize" && paymentMethodCode === "card") {
            this._lw_ccShowDisclosure();
        } else {
            this._lw_ccHideRow();
        }
    },

    /**
     * React to card number entry (debounced): quote once 6+ digits are typed.
     *
     * @private
     * @return {void}
     */
    async _lw_ccOnCardInput() {
        if (!this._lw_ccIsInvoiceFlow()) {
            return;
        }
        const digits = this._lw_ccCardDigits();
        if (digits.length < 6) {
            // Not enough of the card number yet: plain disclosure.
            this._lw_ccShowDisclosure();
            return;
        }
        const seq = this._lw_ccNextSeq();
        const quote = await this._lw_ccQuote(digits, null);
        if (seq !== this._lw_ccQuoteSeq) {
            return; // A newer keystroke already re-quoted.
        }
        this._lw_ccApplyQuote(quote);
    },

    // #=== QUOTE RPC ===#

    /**
     * Call the quote route and return its response.
     *
     * The params always include invoice_id / access_token / card_bin / overdue
     * so the call shape is stable across the single-invoice, overdue and token
     * paths. The invoice id is parsed from the tail of the transaction route.
     *
     * @private
     * @param {string} cardBin - The card number typed so far (digits, any length).
     * @param {number|null} tokenId - The id of the selected token, if any.
     * @return {object|null} The quote response, or null when the call failed
     * (the caller decides whether that means "no fee" or "fail open").
     */
    async _lw_ccQuote(cardBin, tokenId) {
        const route = this.paymentContext["transactionRoute"] || "";
        const params = {
            "invoice_id": 0,
            "access_token": this.paymentContext["accessToken"] || "",
            "card_bin": String(cardBin || "").replace(/\D/g, "").slice(0, 6),
            "overdue": route.endsWith("/invoice/transaction/overdue"),
        };
        const idMatch = route.match(/\/invoice\/transaction\/(\d+)/);
        if (idMatch) {
            params["invoice_id"] = parseInt(idMatch[1], 10);
        }
        if (tokenId) {
            params["token_id"] = tokenId;
        }
        try {
            return await this.waitFor(rpc(LW_CC_QUOTE_ROUTE, params));
        } catch (error) {
            console.warn("[lw_cc_surcharge] quote call failed", error);
            return null;
        }
    },

    // #=== FEE ROW RENDERING ===#

    /**
     * Show the row with the plain disclosure line only (no amounts).
     *
     * @private
     * @return {void}
     */
    _lw_ccShowDisclosure() {
        const row = this._lw_ccFeeRow();
        if (!row) {
            return;
        }
        row.classList.remove("d-none");
        document.getElementById("lw_cc_surcharge_fee_line")?.classList.add("d-none");
        document.getElementById("lw_cc_surcharge_total_line")?.classList.add("d-none");
        this._lw_ccHideWaiveRow();
        const disclosure = document.getElementById("lw_cc_surcharge_disclosure");
        if (disclosure) {
            disclosure.textContent = LW_CC_DISCLOSURE_PENDING;
        }
    },

    /**
     * Apply a quote response to the fee row: amounts when the fee applies,
     * an explicit no-fee line for debit cards, hidden for any other
     * definitive no, plain disclosure when the call itself failed.
     *
     * Also remembers the last quote that actually applied a fee, so the
     * waive checkbox can restore it when unchecked.
     *
     * @private
     * @param {object|null} quote - The quote response, or null when the rpc
     * failed (fail open).
     * @return {void}
     */
    _lw_ccApplyQuote(quote) {
        this._lw_ccLastAppliedQuote = (quote && quote.applies) ? quote : null;
        if (quote && quote.applies) {
            this._lw_ccRenderQuote(quote);
        } else if (quote && (quote.reason === "debit" || quote.reason === "unknown")) {
            // A card we cannot classify is never surcharged, exactly like a
            // debit card, so say so. Hiding the row instead would make the
            // "a fee may apply" line vanish the moment the customer finishes
            // typing, leaving them at the Pay button with no statement at all.
            this._lw_ccRenderNoFee();
        } else if (quote) {
            this._lw_ccHideRow();
        } else {
            this._lw_ccShowDisclosure(); // rpc failed, fail open
        }
        // A re-quote (e.g. on submit) re-renders the fee row from scratch;
        // reapply the cosmetic waived state if staff already checked the
        // box, so a re-quote does not silently un-waive the display.
        const waiveCheckbox = document.getElementById("lw_cc_surcharge_waive_checkbox");
        if (waiveCheckbox && waiveCheckbox.checked) {
            this._lw_ccOnWaiveToggle(true);
        }
    },

    /**
     * Fill the row from a quote response: disclosure sentence, fee, new total.
     *
     * @private
     * @param {object} quote - The quote response with pct, fee and total.
     * @return {void}
     */
    _lw_ccRenderQuote(quote) {
        const row = this._lw_ccFeeRow();
        if (!row) {
            return;
        }
        const pct = this._lw_ccFormatNumber(quote.pct, 2);
        const fee = this._lw_ccFormatAmount(quote.fee, quote);
        const total = this._lw_ccFormatAmount(quote.total, quote);
        const disclosure = document.getElementById("lw_cc_surcharge_disclosure");
        if (disclosure) {
            disclosure.textContent = `A ${pct}% credit card fee applies to this payment.`;
        }
        const feeLabel = document.getElementById("lw_cc_surcharge_fee_label");
        if (feeLabel) {
            feeLabel.textContent = `Credit card fee (${pct}%)`;
        }
        const feeAmount = document.getElementById("lw_cc_surcharge_fee_amount");
        if (feeAmount) {
            feeAmount.textContent = fee ? `+ ${fee}` : "";
        }
        const totalAmount = document.getElementById("lw_cc_surcharge_total_amount");
        if (totalAmount) {
            totalAmount.textContent = total;
        }
        document.getElementById("lw_cc_surcharge_fee_line")?.classList.remove("d-none");
        document.getElementById("lw_cc_surcharge_total_line")?.classList.remove("d-none");
        row.classList.remove("d-none");
        this._lw_ccShowWaiveRow();
    },

    /**
     * Show the row with the explicit no-fee line for a debit card.
     *
     * A hidden row after typing a debit card number reads the same as a
     * failure; the positive "no fee" line makes the posture visible.
     *
     * @private
     * @return {void}
     */
    _lw_ccRenderNoFee() {
        const row = this._lw_ccFeeRow();
        if (!row) {
            return;
        }
        row.classList.remove("d-none");
        document.getElementById("lw_cc_surcharge_fee_line")?.classList.add("d-none");
        document.getElementById("lw_cc_surcharge_total_line")?.classList.add("d-none");
        this._lw_ccHideWaiveRow(); // nothing to waive: no fee applies
        const disclosure = document.getElementById("lw_cc_surcharge_disclosure");
        if (disclosure) {
            disclosure.textContent = "No credit card fee applies to this card.";
        }
    },

    /**
     * Hide the row entirely (non-card payment option, or a definitive
     * "fee does not apply" quote response).
     *
     * @private
     * @return {void}
     */
    _lw_ccHideRow() {
        this._lw_ccHideWaiveRow();
        this._lw_ccFeeRow()?.classList.add("d-none");
    },

    // #=== IMPERSONATED-STAFF WAIVE ===#
    //
    // The waive row/checkbox only exist in the DOM at all for an
    // impersonated (login-as) portal session -- the template gates the
    // node server-side on request.session.get('impersonate_from_uid'), so
    // a real customer's markup never contains it and every helper below
    // is a safe no-op (optional chaining on a null lookup) on their page.
    // Checking the box is presentation only: the server independently
    // re-authorizes the waive against the same session key in
    // _lw_cc_uplift_transaction_kwargs before it does anything.

    /**
     * Show the waive row (a fee currently applies and can be waived).
     *
     * @private
     * @return {void}
     */
    _lw_ccShowWaiveRow() {
        document.getElementById("lw_cc_surcharge_waive_row")?.classList.remove("d-none");
    },

    /**
     * Hide the waive row and reset the checkbox.
     *
     * Called whenever the fee row no longer represents a live, waivable
     * fee (disclosure-only, no-fee, or hidden states) so a stale checked
     * box from a previous card selection cannot leak a waive request for
     * a card that no longer carries a fee.
     *
     * @private
     * @return {void}
     */
    _lw_ccHideWaiveRow() {
        document.getElementById("lw_cc_surcharge_waive_row")?.classList.add("d-none");
        const waiveCheckbox = document.getElementById("lw_cc_surcharge_waive_checkbox");
        if (waiveCheckbox) {
            waiveCheckbox.checked = false;
        }
    },

    /**
     * Bind the waive checkbox's change listener, once.
     *
     * The row is server-rendered and static (not created/destroyed by
     * this script), so binding once is sufficient; the row is only ever
     * shown/hidden via CSS classes afterward.
     *
     * @private
     * @return {void}
     */
    _lw_ccBindWaiveCheckbox() {
        const waiveCheckbox = document.getElementById("lw_cc_surcharge_waive_checkbox");
        if (!waiveCheckbox || waiveCheckbox.dataset["lw_ccWaiveBound"] === "1") {
            return;
        }
        waiveCheckbox.dataset["lw_ccWaiveBound"] = "1";
        waiveCheckbox.addEventListener("change", () => {
            this._lw_ccOnWaiveToggle(waiveCheckbox.checked);
        });
    },

    /**
     * Cosmetically reflect the waive checkbox on the fee row.
     *
     * Purely a display concern: checking the box hides the fee/total
     * amounts and swaps in a "waived" sentence; unchecking restores the
     * last applied quote (or the plain disclosure if none was applied
     * yet). The server holds the actual authority over whether the waive
     * is honored -- see _lw_cc_uplift_transaction_kwargs.
     *
     * @private
     * @param {boolean} checked
     * @return {void}
     */
    _lw_ccOnWaiveToggle(checked) {
        const row = this._lw_ccFeeRow();
        if (!row) {
            return;
        }
        if (checked) {
            document.getElementById("lw_cc_surcharge_fee_line")?.classList.add("d-none");
            document.getElementById("lw_cc_surcharge_total_line")?.classList.add("d-none");
            const disclosure = document.getElementById("lw_cc_surcharge_disclosure");
            if (disclosure) {
                disclosure.textContent = "Credit card fee waived for this payment.";
            }
        } else if (this._lw_ccLastAppliedQuote) {
            this._lw_ccRenderQuote(this._lw_ccLastAppliedQuote);
        } else {
            this._lw_ccShowDisclosure();
        }
    },

    // #=== DOM HELPERS ===#

    /**
     * Return the fee row element, or null when the template did not render it
     * (non-invoice flow) or the DOM is not ready.
     *
     * @private
     * @return {HTMLElement|null}
     */
    _lw_ccFeeRow() {
        return document.getElementById("lw_cc_surcharge_fee_row");
    },

    /**
     * Return the Authorize.net card number input of the selected payment
     * option's inline form, or null.
     *
     * Scoped through the checked radio's inline form so a second inline form
     * (e.g. a saved token's) cannot shadow the input despite the shared id.
     *
     * @private
     * @return {HTMLInputElement|null}
     */
    _lw_ccCardInput() {
        const checkedRadio = this.el.querySelector('input[name="o_payment_radio"]:checked');
        const inlineForm = checkedRadio ? this._getInlineForm(checkedRadio) : null;
        return inlineForm?.querySelector("#o_authorize_card")
            || document.getElementById("o_authorize_card");
    },

    /**
     * Return the digits typed so far in the card number input.
     *
     * @private
     * @return {string}
     */
    _lw_ccCardDigits() {
        const cardInput = this._lw_ccCardInput();
        if (!cardInput) {
            return "";
        }
        return String(cardInput.value || "").replace(/\D/g, "");
    },

    /**
     * Attach the debounced input listener to the card number input, once.
     *
     * @private
     * @return {void}
     */
    _lw_ccBindCardInput() {
        const cardInput = this._lw_ccCardInput();
        if (!cardInput || cardInput.dataset["lw_ccBinBound"] === "1") {
            return;
        }
        cardInput.dataset["lw_ccBinBound"] = "1";
        let debounceTimer = null;
        cardInput.addEventListener("input", () => {
            if (debounceTimer) {
                clearTimeout(debounceTimer);
            }
            debounceTimer = setTimeout(() => {
                debounceTimer = null;
                this._lw_ccOnCardInput().catch((error) => {
                    console.warn("[lw_cc_surcharge] card input quote failed", error);
                    this._lw_ccShowDisclosure();
                });
            }, 200);
        });
    },

    // #=== MISC HELPERS ===#

    /**
     * Check whether the form pays an invoice (pay modal or payment link).
     *
     * @private
     * @return {boolean}
     */
    _lw_ccIsInvoiceFlow() {
        const route = this.paymentContext["transactionRoute"];
        return Boolean(route) && route.includes(LW_CC_INVOICE_ROUTE_MARKER);
    },

    /**
     * Increment and return the quote sequence counter, used to drop responses
     * that resolve after a newer quote was started.
     *
     * @private
     * @return {number}
     */
    _lw_ccNextSeq() {
        this._lw_ccQuoteSeq = (this._lw_ccQuoteSeq || 0) + 1;
        return this._lw_ccQuoteSeq;
    },

    /**
     * Format a quote number with fixed decimals, or '' when not a number.
     *
     * @private
     * @param {number} value - The value from the quote response.
     * @param {number} decimals - The number of decimals to keep.
     * @return {string}
     */
    _lw_ccFormatNumber(value, decimals) {
        const parsed = parseFloat(value);
        if (!isFinite(parsed)) {
            return "";
        }
        return parsed.toFixed(decimals);
    },

    /**
     * Format a quote amount with the currency from the quote response.
     *
     * Alphabetic currency codes (e.g. "USD") are separated from the amount by
     * a non-breaking space, signs (e.g. "$") are not. Falls back to the bare
     * amount when the response carries no symbol.
     *
     * @private
     * @param {number} value - The amount from the quote response.
     * @param {object} quote - The quote response, carrying currency_symbol and
     * optionally currency_position ('before' the default, or 'after').
     * @return {string}
     */
    _lw_ccFormatAmount(value, quote) {
        const amount = this._lw_ccFormatNumber(value, 2);
        if (amount === "") {
            return "";
        }
        const symbol = quote && quote.currency_symbol ? String(quote.currency_symbol) : "";
        if (!symbol) {
            return amount;
        }
        const separator = /[A-Za-z]/.test(symbol) ? "\u00A0" : "";
        return quote.currency_position === "after"
            ? `${amount}${separator}${symbol}`
            : `${symbol}${separator}${amount}`;
    },

});
