# -*- coding: utf-8 -*-
from odoo.tests import tagged

from odoo.addons.payment_authorize.tests.common import AuthorizeCommon


@tagged('post_install', '-at_install', 'lw_authorize_address_truncate')
class TestAddressTruncate(AuthorizeCommon):

    def test_over_60_char_address_is_clamped_to_60_and_rstripped(self):
        """A combined street + street2 address over Authorize.Net's 60-char billTo limit must be
        hard-sliced to exactly 60 chars, then rstripped if the slice lands on trailing whitespace."""
        # 59 'A's + a space (the format_partner_address() join point) + 10 'B's = 70 chars total.
        # Slicing the first 60 chars stops exactly on that space, so rstrip() must trim it.
        values = {
            'partner_address': ('A' * 59) + ' ' + ('B' * 10),
            'partner_id': self.partner.id,
        }
        result = self.env['payment.transaction']._get_specific_create_values('authorize', values)

        self.assertIn('partner_address', result)
        self.assertEqual(result['partner_address'], 'A' * 59)
        self.assertEqual(len(result['partner_address']), 59)

    def test_short_address_is_left_untouched(self):
        """An address at or under the 60-char limit must not appear in the returned dict at all -
        no clamp key churn for values that were never over the limit."""
        values = {
            'partner_address': 'Huron St 8',
            'partner_id': self.partner.id,
        }
        result = self.env['payment.transaction']._get_specific_create_values('authorize', values)

        self.assertNotIn('partner_address', result)

    def test_city_over_40_and_zip_over_20_are_clamped(self):
        """City (40-char limit) and zip (20-char limit) clamp the same way as address."""
        values = {
            'partner_city': 'C' * 45,
            'partner_zip': '1' * 25,
            'partner_id': self.partner.id,
        }
        result = self.env['payment.transaction']._get_specific_create_values('authorize', values)

        self.assertEqual(result['partner_city'], 'C' * 40)
        self.assertEqual(result['partner_zip'], '1' * 20)

    def test_non_authorize_provider_values_are_untouched(self):
        """Providers other than authorize must not have their address fields clamped at all - this
        hook returns whatever super() returned, unchanged."""
        values = {
            'partner_address': 'X' * 100,
            'partner_city': 'Y' * 100,
            'partner_zip': 'Z' * 100,
            'partner_id': self.partner.id,
        }
        result = self.env['payment.transaction']._get_specific_create_values('none', values)

        self.assertEqual(result, {})

    def test_missing_and_false_keys_are_tolerated(self):
        """A `values` dict with a missing or False partner_address/city/zip must not raise - the
        clamp is a no-op for keys that aren't there."""
        values = {'partner_address': False, 'partner_id': self.partner.id}
        result = self.env['payment.transaction']._get_specific_create_values('authorize', values)
        self.assertEqual(result, {})

        result = self.env['payment.transaction']._get_specific_create_values('authorize', {})
        self.assertEqual(result, {})

    def test_end_to_end_create_clamps_real_partner_address(self):
        """Integration check: a real transaction create() for a partner whose combined street +
        street2 is over 60 chars (mirrors a production failure: street 30 + street2 33
        = 64 chars) ends up with a clamped partner_address on the created record, proving the hook
        actually runs after core's partner-derived value population and not before."""
        long_partner = self.env['res.partner'].create({
            'name': 'Long Address Test Partner',
            'street': 'A' * 30,
            'street2': 'B' * 33,
            'city': 'Test City',
            'zip': '00000',
            'country_id': self.country_belgium.id,
        })
        self.assertEqual(len(long_partner.street) + 1 + len(long_partner.street2), 64)

        tx = self._create_transaction('direct', partner_id=long_partner.id)

        self.assertEqual(len(tx.partner_address), 60)
        self.assertEqual(tx.partner_address, ('A' * 30) + ' ' + ('B' * 29))
