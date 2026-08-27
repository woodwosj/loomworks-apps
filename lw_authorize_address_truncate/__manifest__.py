# -*- coding: utf-8 -*-
{
    'name': 'Authorize.Net Address Truncate',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Payment Providers',
    'summary': "Clamp billTo address fields Authorize.Net rejects when they exceed its schema limits",
    'description': """
Authorize.Net rejects any transaction whose billTo address block exceeds its XML schema's per-field
length limits (address 60 chars, city 40 chars, zip 20 chars) with a hard schema validation failure
BEFORE the card is ever attempted. The customer just sees an unexplained failed payment; nothing in
Odoo surfaces why.

Root cause: core concatenates ``street`` + ``street2`` into ``payment.transaction.partner_address``
as-is (``payment.transaction.create()`` builds it via
``payment_utils.format_partner_address(partner.street, partner.street2)``) with no length guard.
Any partner whose combined street address (or city, or zip) is long enough trips the gateway's
validation, not Odoo's.

This module overrides ``_get_specific_create_values()`` rather than pre-clamping values before
``super().create()`` runs: core's ``create()`` populates the partner-derived values (including
``partner_address``, ``partner_city``, ``partner_zip``) into the create ``values`` dict and only
THEN calls ``values.update(self._get_specific_create_values(provider.code, values))`` - clamping
before that point would be silently overwritten by the partner-derived values a few lines later.
When the provider is ``authorize``, any of ``partner_address`` (60), ``partner_city`` (40), or
``partner_zip`` (20) that is over its limit is hard-sliced to the limit and right-stripped; values
already within the limit are left untouched.
""",
    'author': 'Loomworks Solutions LLC',
    'website': 'https://loomworks.solutions',
    'support': 'stephen@loomworks.dev',
    'license': 'LGPL-3',
    'price': 0,
    'images': ['static/description/screenshot_1.png', 'static/description/screenshot_2.png', 'static/description/screenshot_3.png', 'static/description/screenshot_4.png', 'static/description/hero.png', 'static/description/flow.png'],
    'depends': ['payment_authorize'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
