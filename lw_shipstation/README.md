# Loomworks ShipStation Integration

Connects Odoo outbound deliveries to ShipStation and imports tracking
numbers back onto `stock.picking`. Mixes two API generations:

- **V1 (legacy)** — `ssapi.shipstation.com`, Basic auth, polled every
  15 minutes by the crons in `data/ir_cron.xml`. Used for outbound
  order export and the historical tracking-import path. Credentials
  live on `res.company` (`shipstation_api_key`,
  `shipstation_api_secret`, etc.) and are configured via
  Inventory → Configuration → Settings → ShipStation Integration.

- **V2 (added 19.0.3.0.0)** — `api.shipstation.com`, `API-Key` header
  auth. Inbound tracking arrives via the `fulfillment_shipped_v2`
  webhook at `POST /shipstation/webhook/v2`. A 30-min poll fallback
  (`data/cron_poll.xml`) hits `GET /v2/shipments?shipped_after=<watermark>`
  to reconcile any picking whose webhook was missed.

The two paths coexist: V1 keeps working for outbound export; V2 adds
near-real-time inbound tracking.

## Configuration — `ir.config_parameter` keys (V2)

All four are read via the standard Odoo system parameters mechanism.
Set them via Settings → Technical → Parameters → System Parameters or
via `odoo-bin shell`.

| Key                                                  | Required | Default                              | Purpose                                                                                          |
|------------------------------------------------------|----------|--------------------------------------|--------------------------------------------------------------------------------------------------|
| `lw_shipstation.webhook_secret`                | Yes      | (none)                               | Shared secret used for inbound webhook verification. Generate via `openssl rand -hex 32`.        |
| `lw_shipstation.api_key`                       | Yes      | (none)                               | ShipStation V2 API key (`app.shipstation.com → Settings → API Management → API Keys`).           |
| `lw_shipstation.webhook_secret_header`         | No       | `X-ShipStation-Webhook-Secret`       | Name of the custom HTTP header carrying the shared secret on inbound webhooks.                   |
| `lw_shipstation.webhook_max_body_bytes`        | No       | `65536`                              | Hard size cap on inbound webhook bodies. Anything larger is rejected with HTTP 413 before parse. |
| `lw_shipstation.webhook_max_skew_seconds`      | No       | `0` (disabled)                       | Replay-tolerance window. If non-zero, payloads whose `ship_date` is older than this are rejected.|

**Do NOT commit real secrets to this repo.** The values above are
placeholders; the operator sets them per environment.

## Signature verification — design note

ShipStation V2 webhooks do **not** ship a native body-signed HMAC
header. The standard integration pattern (confirmed against
`docs.shipstation.com/openapi`, retrieved 2026-05-18) is to provision
a custom header on the webhook record and forward a shared secret.
This controller satisfies the "HMAC SHA-256
signature verification" requirement via a constant-time
`hmac.compare_digest()` of the value in
`X-ShipStation-Webhook-Secret` against the stored ICP secret.

For forward-compatibility, the controller **also** accepts an
`X-SS-Signature: sha256=<hex>` header. When present, it is verified
as a true HMAC-SHA256 of the raw request body, keyed on the same
shared secret. ShipStation does not natively emit this today; this
hook lets us migrate to body-signed HMAC the moment it does, without
changing the route shape or adding a second secret.

The request is rejected with HTTP 401 if neither path validates.

## Provisioning the webhook in ShipStation

Once the ICP keys above are populated on a deployment, register the
webhook in ShipStation:

1. Sign in to `app.shipstation.com`.
2. Settings → API Management → Webhooks → **Add Webhook**.
3. Event: `fulfillment_shipped_v2`.
4. URL: `https://<your-host>/shipstation/webhook/v2`.
   - Production: `https://<your-pod>.odoo.com/shipstation/webhook/v2`
   - Staging:    `https://<staging-pod>.dev.odoo.com/shipstation/webhook/v2`
5. Custom header:
   - Key:   `X-ShipStation-Webhook-Secret`
   - Value: the exact value stored in
            `lw_shipstation.webhook_secret`.

The V2 poll fallback is automatic — once `lw_shipstation.api_key`
is set, the 30-min cron defined in `data/cron_poll.xml` activates on
the next tick.

## Secret rotation

1. Generate a new value: `openssl rand -hex 32`.
2. Update the ICP key:
   ```
   env['ir.config_parameter'].sudo().set_param(
       'lw_shipstation.webhook_secret',
       '<the-new-value>',
   )
   ```
3. Update the webhook record in ShipStation admin to forward the new
   header value.

The controller re-reads the ICP value on every inbound request, so
rotation is atomic from the Odoo side — no app restart, no cron tick,
no module upgrade. ShipStation processes webhook header changes
immediately on save.

## Smoke test (curl)

```bash
SECRET="<value-from-ir.config_parameter>"
HOST="https://<your-host>"

# Happy path
curl -i -X POST "$HOST/shipstation/webhook/v2" \
  -H 'Content-Type: application/json' \
  -H "X-ShipStation-Webhook-Secret: $SECRET" \
  -d '{
        "event": "fulfillment_shipped_v2",
        "data": {
          "order_number": "S00001",
          "tracking_number": "1Z999AA10123456784",
          "carrier_code": "ups",
          "ship_date": "2026-05-18T18:00:00Z",
          "shipment_id": "se-12345"
        }
      }'
# Expect: 200 {"status":"ok","matched":true,"picking":"WH/OUT/..."}

# Invalid secret
curl -i -X POST "$HOST/shipstation/webhook/v2" \
  -H 'Content-Type: application/json' \
  -H 'X-ShipStation-Webhook-Secret: WRONG' \
  -d '{"event":"fulfillment_shipped_v2","data":{"order_number":"S00001","tracking_number":"X"}}'
# Expect: 401 {"error":"invalid signature"}
```
