# Local vertical-slice demo

This walkthrough proves both the normal path and the complete
failure-repair-replay path:

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

## 6. Force a terminal failure

Change Receiver Lab to return a permanent HTTP `400` response. A permanent
failure is useful here because it reaches the dead-letter state deterministically
without waiting through the retry schedule.

```powershell
$failingReceiver = @{
  mode = "permanent_failure"
  failures_before_success = 0
  delay_ms = 0
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Put `
  -Uri "http://localhost:8100/control" `
  -ContentType "application/json" `
  -Body $failingReceiver

$failedEventRequest = @{
  endpoint_id = $endpoint.id
  type = "demo.invoice.failed"
  data = @{
    invoice_id = "invoice-replay-1"
    amount = 9900
  }
} | ConvertTo-Json -Depth 5

$failedEvent = Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/v1/events" `
  -ContentType "application/json" `
  -Headers @{ "Idempotency-Key" = "demo-invoice-replay-1" } `
  -Body $failedEventRequest

do {
  Start-Sleep -Milliseconds 250
  $failedEventDetail = Invoke-RestMethod `
    -Uri ("http://localhost:8000/v1/events/" + $failedEvent.event_id)
  $sourceDelivery = $failedEventDetail.deliveries | `
    Where-Object { $_.id -eq $failedEvent.delivery_id }
} while ($sourceDelivery.status -ne "dead_lettered")

$sourceAttemptsBeforeReplay = Invoke-RestMethod `
  -Uri ("http://localhost:8000/v1/deliveries/" + $failedEvent.delivery_id + "/attempts")

$sourceAttemptsBeforeReplay | ConvertTo-Json -Depth 10
```

The source delivery should now be `dead_lettered` with one completed,
terminal-failure attempt containing HTTP `400`.

## 7. Repair the receiver and approve replay

Repairing the destination does not mutate the dead-lettered delivery. The
manual replay request creates generation 1 as a new pending delivery.

```powershell
$repairedReceiver = @{
  mode = "success"
  failures_before_success = 0
  delay_ms = 0
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Put `
  -Uri "http://localhost:8100/control" `
  -ContentType "application/json" `
  -Body $repairedReceiver

$replayHeaders = @{ "Idempotency-Key" = "repair-invoice-replay-1" }
$replay = Invoke-RestMethod `
  -Method Post `
  -Uri ("http://localhost:8000/v1/deliveries/" + $failedEvent.delivery_id + "/replays") `
  -Headers $replayHeaders

# Sending the same approval again is safe and returns the same replay delivery.
$sameReplay = Invoke-RestMethod `
  -Method Post `
  -Uri ("http://localhost:8000/v1/deliveries/" + $failedEvent.delivery_id + "/replays") `
  -Headers $replayHeaders

$replay
$sameReplay
```

Both responses should contain the same `delivery_id` and
`replay_generation: 1`.

## 8. Prove the replay succeeded without erasing history

```powershell
do {
  Start-Sleep -Milliseconds 250
  $replayAttempts = Invoke-RestMethod `
    -Uri ("http://localhost:8000/v1/deliveries/" + $replay.delivery_id + "/attempts")
} while ($replayAttempts.delivery.status -ne "delivered")

$sourceAttemptsAfterReplay = Invoke-RestMethod `
  -Uri ("http://localhost:8000/v1/deliveries/" + $failedEvent.delivery_id + "/attempts")

$completeHistory = Invoke-RestMethod `
  -Uri ("http://localhost:8000/v1/events/" + $failedEvent.event_id)

$replayAttempts | ConvertTo-Json -Depth 10
$sourceAttemptsAfterReplay | ConvertTo-Json -Depth 10
$completeHistory | ConvertTo-Json -Depth 10
```

The final evidence should show:

- Generation 0 remains `dead_lettered` with its original HTTP `400` attempt.
- Generation 1 is `delivered` with a separate HTTP `200` attempt.
- Both deliveries reference the same immutable event and endpoint.
- Repeating the replay request did not create generation 2.
