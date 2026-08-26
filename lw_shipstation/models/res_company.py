import base64
import logging
import requests
import time

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

SS_BASE_URL = 'https://ssapi.shipstation.com'


class ResCompany(models.Model):
    _inherit = 'res.company'

    shipstation_api_key = fields.Char(
        string='ShipStation API Key',
        groups='base.group_system',
    )
    shipstation_api_secret = fields.Char(
        string='ShipStation API Secret',
        groups='base.group_system',
    )
    shipstation_enabled = fields.Boolean(
        string='ShipStation Enabled',
        default=False,
    )
    shipstation_auto_export = fields.Boolean(
        string='Auto-Export Ready Pickings',
        default=False,
    )
    shipstation_store_id = fields.Char(
        string='ShipStation Store ID',
        help='Optional: limit exports to a specific ShipStation store.',
    )
    shipstation_web_url = fields.Char(
        string='ShipStation Web URL',
        default='https://ship14.shipstation.com',
        help='Base URL of your ShipStation web app (regional pod, e.g. '
             'https://ship14.shipstation.com). The "Open in ShipStation" '
             'button opens {this}/orders/awaiting-shipment. Change this if '
             'ShipStation reassigns your account to a different pod.',
    )
    ss_last_tracking_sync = fields.Datetime(
        string='Last Tracking Sync',
        help='Timestamp of the last successful tracking import from ShipStation.',
    )
    ss_last_order_import = fields.Datetime(
        string='Last Order Import',
        help='Timestamp of the last successful order import from ShipStation.',
    )

    def _shipstation_api_call(self, method, endpoint, data=None, params=None):
        """
        Make an authenticated API call to ShipStation.

        Args:
            method: HTTP method ('GET', 'POST', 'PUT', 'DELETE')
            endpoint: API endpoint path (e.g. '/orders/createorder')
            data: dict payload for POST/PUT (will be JSON-encoded)
            params: dict of query parameters for GET

        Returns:
            tuple (success: bool, response_data: dict or str)
        """
        self.ensure_one()
        if not self.shipstation_api_key or not self.shipstation_api_secret:
            return (False, 'ShipStation API credentials not configured.')

        auth_string = base64.b64encode(
            f'{self.shipstation_api_key}:{self.shipstation_api_secret}'.encode()
        ).decode()

        headers = {
            'Authorization': f'Basic {auth_string}',
            'Content-Type': 'application/json',
        }

        url = f'{SS_BASE_URL}{endpoint}'
        max_retries = 3

        for attempt in range(max_retries):
            try:
                resp = requests.request(
                    method,
                    url,
                    headers=headers,
                    json=data,
                    params=params,
                    timeout=30,
                )

                # Check rate limiting
                remaining = resp.headers.get('X-Rate-Limit-Remaining')
                if remaining is not None and int(remaining) < 5:
                    _logger.warning(
                        'ShipStation rate limit nearly exhausted: %s remaining',
                        remaining,
                    )
                    # In cron context, caller should commit and exit
                    # In button context, we continue but warn

                # No retry for client errors
                if resp.status_code in (400, 401, 403, 404):
                    _logger.error(
                        'ShipStation API %s %s returned %s: %s',
                        method, endpoint, resp.status_code, resp.text[:500],
                    )
                    return (False, resp.text[:500])

                resp.raise_for_status()

                if resp.content:
                    return (True, resp.json())
                return (True, {})

            except requests.exceptions.RequestException as e:
                _logger.warning(
                    'ShipStation API %s %s attempt %d/%d failed: %s',
                    method, endpoint, attempt + 1, max_retries, e,
                )
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                else:
                    return (False, str(e))

        return (False, 'Max retries exceeded.')

    def action_shipstation_test_connection(self):
        """Test ShipStation API connection by fetching carriers list."""
        self.ensure_one()
        success, result = self._shipstation_api_call('GET', '/carriers')
        if success:
            carrier_names = [c.get('name', '?') for c in result] if isinstance(result, list) else []
            if carrier_names:
                msg = 'Connection successful!\n\nCarriers found: %s' % ', '.join(carrier_names)
            else:
                msg = 'Connection successful! (no carriers returned)'
            raise UserError(msg)
        else:
            raise UserError('Connection failed:\n%s' % result)
