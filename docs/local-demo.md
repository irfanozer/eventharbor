# Local vertical-slice demo

This walkthrough proves the current path:

```text
publisher -> FastAPI -> PostgreSQL -> worker -> Receiver Lab
                                      |
                                      +-> persisted attempt evidence
```

## 1. Start the system

From the repository root:

```powershell
docker compose up --build
```

Wait until PostgreSQL and Receiver Lab are healthy, the migration process exits
with code 0, and the API and worker are running. Leave this terminal open.

If Windows already uses port `5432`, copy `.env.example` to `.env`, set
`POSTGRES_PORT=55432`, and run the same command again. This only changes the
Windows-to-container port; services still find the database at `postgres:5432`.

## 2. Configure a successful receiver

Open a second PowerShell terminal in the repository root:

```powershell
$receiverConfiguration = @{
  mode = "success"
  failures_before_success = 0
  delay_ms = 0
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Put `
  -Uri "http://localhost:8100/control" `
  -ContentType "application/json" `
  -Body $receiverConfiguration
```

## 3. Register Receiver Lab

```powershell
$endpointRequest = @{
  name = "Local Receiver Lab"
  url = "http://receiver-lab:8100/webhooks"
} | ConvertTo-Json

$endpoint = Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/v1/endpoints" `
  -ContentType "application/json" `
  -Body $endpointRequest

$endpoint
```

The response includes a signing secret because this local milestone shows the
secret once at creation. Do not use this storage design in production.

## 4. Publish an event

```powershell
$eventRequest = @{
  endpoint_id = $endpoint.id
  type = "demo.order.created"
  data = @{
    order_id = "order-123"
    amount = 4200
  }
} | ConvertTo-Json -Depth 5

$event = Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/v1/events" `
  -ContentType "application/json" `
  -Headers @{ "Idempotency-Key" = "demo-order-123-created" } `
  -Body $eventRequest

$event
```

The API returns `202` only after the event and its initial delivery are
committed together. Repeating the same request and idempotency key returns the
same event. Reusing the key with different data returns `409`.

## 5. Inspect delivery evidence

Wait one second for the worker, then run:

```powershell
$eventDetail = Invoke-RestMethod `
  -Uri ("http://localhost:8000/v1/events/" + $event.event_id)

$attempts = Invoke-RestMethod `
  -Uri ("http://localhost:8000/v1/deliveries/" + $event.delivery_id + "/attempts")

$received = Invoke-RestMethod -Uri "http://localhost:8100/requests"

$eventDetail | ConvertTo-Json -Depth 10
$attempts | ConvertTo-Json -Depth 10
$received | ConvertTo-Json -Depth 10
```

You should see `delivered`, one `succeeded` attempt with HTTP `200`, and one
Receiver Lab request containing the stable event ID and an HMAC signature. The
event detail also shows `payload_sha256`, the digest of the exact outbound body,
and `request_fingerprint_sha256`, the separate value used for idempotency.

Swagger is also available at <http://localhost:8000/docs> and
<http://localhost:8100/docs>.
