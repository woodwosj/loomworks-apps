{
    "name": "Sale Order Split",
    "summary": "Split a sale order by selected lines into one or more new orders.",
    "version": "19.0.1.0.0",
    "category": "Sales/Sales",
    "description": """
Split any sale order by selected lines.

Key Features:
- Assign order lines to split groups from an editable list view, then
  preview the split in a wizard before confirming
- Stock-aware: only remaining (undelivered) quantities move to the new
  order(s); delivered quantities stay on the original with traceability
- Confirmed orders are handled audit-safely: lines are zeroed and
  annotated instead of deleted (Odoo 19 blocks line deletion)
- New orders get the next split number in the series; pickings reference
  the correct source document
- Full chatter trail on both the original and the new orders
""",
    "author": "Loomworks Solutions LLC",
    "website": "https://loomworks.solutions",
    "support": "apps@loomworks.solutions",
    "license": "OPL-1",
    "price": 9,
    "currency": "USD",
    "images": ["static/description/screenshot_1.png", "static/description/screenshot_2.png", "static/description/screenshot_3.png", "static/description/screenshot_4.png", "static/description/hero.png", "static/description/flow.png"],
    "depends": ["sale", "sale_stock"],
    "data": [
        "security/ir.model.access.csv",
        "views/sale_order_view.xml",
        "views/sale_order_line_view.xml",
        "wizard/lw_sale_split_wizard_view.xml",
    ],
    "installable": True,
    "application": False,
}
