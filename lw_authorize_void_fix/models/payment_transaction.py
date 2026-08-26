# -*- coding: utf-8 -*-
import pprint

from odoo import models

from odoo.addons.payment.logging import get_payment_logger
from odoo.addons.payment_authorize.models.authorize_request import AuthorizeAPI

_logger = get_payment_logger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    def _send_void_request(self):
        """Override of `payment_authorize` to fix the refTransId sent to Authorize.Net.

        Core's ``_send_void_request()`` passes ``self.provider_reference`` to
        ``AuthorizeAPI.void()``, but ``self`` here is the void child
        transaction created by ``_create_child_transaction()``, whose
        ``provider_reference`` is still empty at this point (it is only
        populated by ``_apply_updates()`` after a response comes back). Every
        void request is therefore sent with a blank refTransId and rejected
        by Authorize.Net. Refund and capture already resolve the parent's
        reference the same way this override does; void alone regressed.
        Delete this module (and its one-line diff) if core ever fixes
        `_send_void_request()` upstream.
        """
        if self.provider_code != 'authorize':
            return super()._send_void_request()

        authorize_API = AuthorizeAPI(self.provider_id)
        # The one deviation from core: use the source (parent) transaction's
        # provider reference, falling back to our own in case a void is ever
        # sent without a linked source transaction.
        reference = self.source_transaction_id.provider_reference or self.provider_reference
        res_content = authorize_API.void(reference)
        _logger.info(
            "void request response for transaction %s:\n%s",
            self.reference, pprint.pformat(res_content)
        )
        self._process('authorize', {'response': res_content})
