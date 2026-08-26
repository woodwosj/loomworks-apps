# Retainer Hours Module
# Copyright (C) 2026 Loomworks Solutions LLC
# License OPL-1 (https://www.odoo.com/documentation/master/legal/licenses.html)

{
    'name': 'Retainer Hours & Prepaid Blocks',
    'version': '19.0.1.0.0',
    'category': 'Services',
    'summary': 'Monthly retainer hour tracking with one-cycle rollover, overage billing and portal hours log',
    'description': """
        Track included retainer hours per client per monthly cycle and bill
        overages automatically.

        Key Features:
        - Retainer plans with configurable included hours, in-plan and
          overage rates, cycle start day and rollover policy
        - One-cycle rollover (no banking): unused hours carry over to the
          next cycle only, capped at the monthly allowance
        - Consumptions fed from timesheets (account.analytic.line) or
          manual entry
        - Daily cron closes cycles, snapshots usage and drafts the renewal
          invoice (product line + overage line when applicable)
        - Portal page letting the client see their hours log per cycle
    """,
    'author': 'Loomworks Solutions LLC',
    'website': 'https://loomworks.solutions',
    'support': 'apps@loomworks.solutions',
    'license': 'OPL-1',
    'price': 69,
    'currency': 'USD',
    'images': ['static/description/screenshot_1.png', 'static/description/screenshot_2.png', 'static/description/screenshot_3.png', 'static/description/screenshot_4.png', 'static/description/screenshot_5.png', 'static/description/hero.png', 'static/description/flow.png'],
    'depends': [
        'base',
        'sale_management',
        'project',
        'account',
        'portal',
        'mail',
    ],
    'data': [
        'security/ir.model.access.csv',
        'security/retainer_security.xml',
        'data/retainer_cron.xml',
        'views/retainer_plan_views.xml',
        'views/retainer_consumption_views.xml',
        'views/retainer_cycle_summary_views.xml',
        'views/retainer_menus.xml',
        'templates/portal_retainer_hours.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
