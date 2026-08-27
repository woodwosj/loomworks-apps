{
    'name': 'Portal Invoice CSV Download',
    'version': '19.0.1.0.0',
    'category': 'Accounting',
    'summary': 'Add CSV download (incl. barcode/UPC) alongside PDF on customer portal invoice page',
    'description': """
Portal Invoice CSV Download
============================

On the customer portal invoice page (``/my/invoices/<id>``), the Download
button by default only emits PDF. This module:

- Adds a new portal route ``/my/invoices/<id>/csv`` that returns invoice
  lines as CSV with all standard fields + each line's product barcode/UPC,
  ready for upload into the customer's external software.
- Replaces the single Download button on the portal invoice page with a
  PDF / CSV dropdown.
- Reuses the existing portal access-token auth (the same _document_check_access
  guard upstream uses for the PDF route) so the CSV is gated identically.

Purely additive — no upstream files modified.
""",
    'author': 'Loomworks Solutions LLC',
    'website': 'https://loomworks.solutions',
    'support': 'stephen@loomworks.dev',
    'license': 'LGPL-3',
    'price': 0,
    'images': ['static/description/screenshot_1.png', 'static/description/screenshot_2.png', 'static/description/screenshot_3.png', 'static/description/hero.png', 'static/description/flow.png'],
    'depends': ['account', 'portal'],
    'data': [
        'views/portal_templates.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
