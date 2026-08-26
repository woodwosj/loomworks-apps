# -*- coding: utf-8 -*-
from odoo import models

from odoo.addons.payment.logging import get_payment_logger

_logger = get_payment_logger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    def _void(self, amount_to_void=None):
        """Override of `payment` to tokenize validation transactions before voiding.

        Core voids the penny authorization as the last step of
        ``_apply_updates()``, before ``_process()`` reaches ``_tokenize()``.
        Since odoo/odoo@b557f74d the void actually succeeds, so the customer
        profile request (``createCustomerProfileFromTransactionRequest``) then
        runs against a voided transaction and Authorize.Net returns no payment
        data — the card is never saved.

        Tokenizing here, from the still-active authorization, restores the
        card save. The void then proceeds normally, cleaning up the penny
        authorization. ``_tokenize()`` sets ``tokenize=False`` on success, so
        the later ``_process()``-driven tokenization is a no-op. authorize's
        ``_extract_token_values()`` reads only ``self`` (never
        ``payment_data``), so the empty dict is safe.
        """
        if (
            len(self) == 1
            and self.provider_code == 'authorize'
            and self.operation == 'validation'
            and self.tokenize
            and not self.token_id
            and self.state == 'authorized'
            and self.provider_reference
        ):
            try:
                self._tokenize({})
            except Exception:  # noqa: BLE001 - never let tokenization block the void
                _logger.exception(
                    "tokenize-before-void failed for transaction %s; "
                    "proceeding with void",
                    self.reference,
                )
                # Consume the one-shot save attempt: with tokenize left True,
                # core _process() would retry _tokenize() right after the void
                # and re-raise UNPROTECTED, rolling back the void child records
                # mid-flow (caught by test_tokenize_failure_does_not_block_void).
                self.tokenize = False
        return super()._void(amount_to_void=amount_to_void)
