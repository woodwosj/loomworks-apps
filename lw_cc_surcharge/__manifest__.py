# -*- coding: utf-8 -*-
{
    'name': 'CC Surcharge Suite',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Accounting',
    'summary': 'Credit card surcharge with portal BIN capture + monthly service charge on past-due AR',
    'description': """
CC Surcharge Suite
==================

One module that combines a credit card surcharge engine with a monthly
service charge on past-due balances.

Credit card surcharge at payment time
-------------------------------------
A percentage fee added when customers on Net 30+ payment terms pay by
credit card. Debit cards are excluded automatically via a self-hosted
BIN (Bank Identification Number) lookup, so debit cardholders are never
charged the fee.

The portal uplift (optional) quotes the fee in the customer payment
modal as soon as the first six card digits are entered or a saved card
is selected, then charges invoice + fee in ONE transaction. The
backend Pay wizard shows a waivable surcharge checkbox when a saved
card token is selected.

Monthly service charge on past-due AR
-------------------------------------
1.5% per month on outstanding AR past due > 30 days (configurable rate,
grace days and minimum balance). A monthly cron computes the charges
and writes a per-invoice audit report; live charge invoices are created
only after Accounting sets a dedicated income account and turns off
Dry-Run Mode.

Safety posture
--------------
The module ships DRY-RUN-ARMED: the monthly cron runs and reports, but
creates zero invoices until an operator deliberately goes live. The
credit card surcharge ships inert until a percentage and applicable
payment terms are configured. No money moves by accident.
    """,
    'author': 'Loomworks Solutions LLC',
    'website': 'https://loomworks.solutions',
    'license': 'OPL-1',
    'price': 129.0,
    'currency': 'USD',
    'support': 'stephen@loomworks.dev',
    'images': ['static/description/screenshot_1.png', 'static/description/screenshot_2.png', 'static/description/screenshot_3.png', 'static/description/screenshot_4.png', 'static/description/screenshot_5.png', 'static/description/screenshot_6.png', 'static/description/hero.png', 'static/description/flow.png'],
    'depends': [
        'account',
        'payment',
        'account_payment',
        'payment_authorize',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/product_data.xml',
        'data/ir_cron_data.xml',
        'data/res_company_data.xml',
        'views/res_company_views.xml',
        'views/res_config_settings_views.xml',
        'views/lw_cc_bin_record_views.xml',
        'views/res_partner_views.xml',
        'views/service_charge_run_views.xml',
        'views/payment_form_templates.xml',
        'views/account_payment_register_views.xml',
    ],
    'assets': {
        'web.assets_frontend': [
            'lw_cc_surcharge/static/src/js/payment_form_quote.js',
        ],
    },
    'post_init_hook': 'post_init_hook',
    'installable': True,
    'application': False,
    'auto_install': False,
}
