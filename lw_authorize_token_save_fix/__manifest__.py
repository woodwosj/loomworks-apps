# -*- coding: utf-8 -*-
{
    'name': 'Authorize.Net Token Save Fix',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Payment Providers',
    'summary': "Tokenize validation transactions before voiding them",
    'description': """
Saving a card stopped working after an upstream fix in ``payment_authorize``
started sending the source transaction's reference on void: every card-save
attempt shows a one-cent authorization in Authorize.Net that is immediately
voided, and the card is never stored ("could not be saved" for portal users;
silent for staff).

Root cause: core's validation flow voids the penny authorization as the
last step of ``_apply_updates()``, BEFORE ``_process()`` reaches
``_tokenize()``. While the void was silently rejected (blank refTransId,
the bug the upstream fix corrected), tokenization ran against a live
authorization and worked. Now that the void succeeds,
``createCustomerProfileFromTransactionRequest`` hits a voided transaction,
Authorize.Net returns no payment data ("unable to create customer payment
profile, data missing from transaction"), and no token is created.

This module overrides ``_void()`` to tokenize validation transactions from
the still-active authorization first, then proceed with the normal void.
``_tokenize()`` flips ``tokenize`` to False on success, so the later
``_process()``-driven tokenization is a no-op. A tokenization failure is
logged and never blocks the void.

Delete this module if upstream reorders tokenization before the validation
void in ``payment``/``payment_authorize``.
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
