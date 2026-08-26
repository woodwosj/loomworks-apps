# -*- coding: utf-8 -*-
from odoo import api, models

# Authorize.Net's billTo schema limits, in characters.
_ADDRESS_FIELD_LIMITS = {
    'partner_address': 60,
    'partner_city': 40,
    'partner_zip': 20,
}


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    @api.model
    def _get_specific_create_values(self, provider_code, values):
        """Override of `payment` to clamp billTo address fields Authorize.Net rejects when oversized.

        Authorize.Net's XML schema validation rejects any transaction whose billTo address block
        exceeds its per-field length limits (address 60, city 40, zip 20) with a hard schema failure
        BEFORE the card is even attempted, leaving the customer looking at an unexplained failed
        payment. Core concatenates ``street`` + ``street2`` into ``partner_address`` as-is (see
        ``payment.transaction.create()``, which builds ``values['partner_address']`` via
        ``payment_utils.format_partner_address()``) with no length guard, so any partner whose
        combined address (or city/zip) is long enough trips the gateway's validation, not Odoo's -
        no error surfaces in Odoo, no card decline, just a rejected request.

        This clamps inside ``_get_specific_create_values()`` rather than pre-clamping ``values``
        before ``super().create()`` runs: core's ``create()`` populates the partner-derived values
        (including ``partner_address``, ``partner_city``, ``partner_zip``) into ``values`` and only
        THEN calls ``values.update(self._get_specific_create_values(provider.code, values))`` -
        clamping any earlier would be silently overwritten by that partner-derived population a few
        lines later. This hook runs after that population, so ``values`` here already holds the
        final partner-derived strings that need clamping.

        Coverage by design: ``create()`` is the only path that ever populates ``partner_address``,
        ``partner_city``, and ``partner_zip`` on a transaction for the authorize flow - these fields
        are never written to post-create, so no write() guard is needed here. Odoo's tokenization /
        saved-card ("profile") flow sends no billTo block at all (Authorize.Net looks the stored
        profile's address up server-side), and a token-based transaction likewise skips billTo, so
        neither path needs this clamp. ``partner_state_id`` / ``partner_country_id`` are m2o fields
        resolved to their gateway names at request-build time rather than sent as raw
        partner_address-style strings, and every seed partner's state/country name already fits
        Authorize.Net's limits, so those are out of scope here. Name/company field enrichment and
        surfacing a friendly error back to the customer when a clamp actually trims real address
        content are DEFERRED - this is a single-purpose live-money fix for the schema-validation
        failures already hitting production (several partners with long addresses), not a general
        address-handling rewrite.
        """
        specific_values = super()._get_specific_create_values(provider_code, values)
        if provider_code != 'authorize':
            return specific_values

        for field_name, max_length in _ADDRESS_FIELD_LIMITS.items():
            value = values.get(field_name)
            if value and len(value) > max_length:
                specific_values[field_name] = value[:max_length].rstrip()

        return specific_values
