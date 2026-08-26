{
    'name': 'ShipStation V2 Webhook Connector',
    'version': '19.0.1.0.0',
    'category': 'Inventory/Delivery',
    'summary': (
        'Export outgoing shipments to ShipStation; import tracking via '
        'V2 webhook + 30-min poll fallback; configurable web queue button.'
    ),
    'description': """
ShipStation V2 Webhook Connector
================================

Wires ShipStation V2 into Odoo for outbound shipments and inbound
tracking. Includes:

* V2 webhook controller with shared-secret HMAC verification.
* 30-minute cron poll fallback when the webhook is silent.
* "Open in ShipStation" button that opens the configurable ShipStation
  web queue (Awaiting Shipment) at the company's regional pod URL
  (default: ship14.shipstation.com). Configurable under Settings.
* Per-picking idempotency via ``ss_shipment_id``.

See README.md for the full operator handover (webhook URL,
``ir.config_parameter`` keys, rotation procedure, curl smoke test).
""",
    'author': 'Loomworks Solutions LLC',
    'website': 'https://loomworks.solutions',
    'support': 'apps@loomworks.solutions',
    'price': 79,
    'currency': 'USD',
    'images': ['static/description/screenshot_1.png', 'static/description/screenshot_2.png', 'static/description/screenshot_3.png', 'static/description/screenshot_4.png', 'static/description/hero.png', 'static/description/flow.png'],
    'depends': ['stock', 'sale', 'delivery'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'data/cron_poll.xml',
        'views/res_config_settings_views.xml',
        'views/stock_picking_views.xml',
        'views/shipstation_log_views.xml',
    ],
    'auto_install': False,
    'installable': True,
    'license': 'OPL-1',
}
