# -*- coding: utf-8 -*-
{
    'name': 'Authorize.Net Void Fix',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Payment Providers',
    'summary': "Fix post-validation void sending an empty refTransId to Authorize.Net",
    'description': """
Every void issued against an Authorize.Net transaction after the source
transaction has already been validated/authorized (a "P-" prefixed child
transaction, e.g. P-V-*) fails on Authorize.Net's side with "A valid
referenced transaction ID is required".

Root cause: core ``payment_authorize``'s ``_send_void_request()`` calls
``AuthorizeAPI.void(self.provider_reference)`` where ``self`` is the
freshly created void child transaction. Child transactions are created via
``_create_child_transaction()`` with an empty ``provider_reference`` (it is
only ever populated by ``_apply_updates()`` after a request is sent to the
gateway), so every void request is sent with a blank refTransId. Refund and
capture already use ``self.source_transaction_id.provider_reference`` (the
parent's real Authorize.Net transId) for the equivalent lookup; void does
not.

This module overrides ``_send_void_request()`` to use the source
transaction's provider reference, matching the refund/capture pattern,
with a fallback to the child's own reference if no source transaction is
linked.
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
