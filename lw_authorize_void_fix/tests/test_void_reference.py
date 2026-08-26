# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.payment_authorize.tests.common import AuthorizeCommon


@tagged('post_install', '-at_install', 'lw_authorize_void_fix')
class TestVoidReference(AuthorizeCommon):

    @mute_logger('odoo.addons.lw_authorize_void_fix.models.payment_transaction')
    def test_void_uses_source_transaction_reference(self):
        """A void child transaction must void the PARENT's Authorize.Net
        transId, not its own (still-empty) provider_reference."""
        source_tx = self._create_transaction('direct', state='done')
        source_tx.provider_reference = 'PARENT-TRANS-ID-123'

        void_tx = source_tx._create_child_transaction(source_tx.amount)
        self.assertFalse(void_tx.provider_reference)
        self.assertEqual(void_tx.source_transaction_id, source_tx)

        with patch(
            'odoo.addons.lw_authorize_void_fix.models.payment_transaction.AuthorizeAPI.void',
            return_value={'x_response_code': '1', 'x_type': 'void'},
        ) as void_mock, patch(
            'odoo.addons.payment.models.payment_transaction.PaymentTransaction._process'
        ):
            void_tx._send_void_request()

        void_mock.assert_called_once_with('PARENT-TRANS-ID-123')

    @mute_logger('odoo.addons.lw_authorize_void_fix.models.payment_transaction')
    def test_void_falls_back_to_own_reference_without_source(self):
        """If a void tx somehow has no source transaction, fall back to its
        own provider_reference rather than voiding with an empty string."""
        tx = self._create_transaction('direct', state='done')
        tx.provider_reference = 'STANDALONE-TRANS-ID-456'
        self.assertFalse(tx.source_transaction_id)

        with patch(
            'odoo.addons.lw_authorize_void_fix.models.payment_transaction.AuthorizeAPI.void',
            return_value={'x_response_code': '1', 'x_type': 'void'},
        ) as void_mock, patch(
            'odoo.addons.payment.models.payment_transaction.PaymentTransaction._process'
        ):
            tx._send_void_request()

        void_mock.assert_called_once_with('STANDALONE-TRANS-ID-456')
