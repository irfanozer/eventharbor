# Local EventHarbor demo

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

Wait until PostgreSQL, Receiver Lab, API, and frontend are healthy, the migration
process exits with code 0, and the worker is running. Leave this terminal open.

If Windows already uses port `5432`, copy `.env.example` to `.env`, set
`POSTGRES_PORT=55432`, and run the same command again. This only changes the
Windows-to-container port; services still find the database at `postgres:5432`.

If port `3000` is in use, set `FRONTEND_PORT=3001` in `.env` and open
`http://localhost:3001` instead.

## 2. Run the recruiter-first event journey

Open <http://localhost:3000>. Choose one Receiver Lab incident, edit the example
order ID, amount, or note if you want, then choose **Send this event and watch it
move**.

The page collapses the input form and keeps the same event inside one live panel.
Read the large **NOW** status first, then follow the highlighted fixed route:

```text
Browser -> EventHarbor API -> PostgreSQL -> delivery worker -> Receiver Lab
```

The compact history shows every actual response from PostgreSQL attempt rows.
The dark exchange card makes the latest destination, request number, response,
duration, and independent Receiver Lab receipt visually dominant.

Available incidents are:

- **Destination outage:** `503, 503, 503, 503`, stop, repair, then generation 1
  receives `200`.
- **Brief service outage:** `503, 503, 200`; generation 0 recovers automatically.
- **API rate limit:** `429, 429, 200`; the first two responses include
  `Retry-After: 2` and the worker waits accordingly.
- **Invalid request:** one `400`; the worker classifies it as permanent and does
  not make pointless retries.

The default destination-outage choice starts the complete recovery presentation.
The order is synthetic, but the
network and persistence path is real. The browser makes a live request through
Nginx to FastAPI; FastAPI commits the event and its first delivery to PostgreSQL;
the worker reads that delivery and makes actual HTTP requests to the separate
Receiver Lab service. The Control Room finds or creates the built-in Receiver
Lab endpoint, selects the deterministic dead-letter
preset, and publishes a synthetic event with a unique idempotency key. These are
real FastAPI calls that create PostgreSQL records. The worker then makes four
real HTTP requests to Receiver Lab, and each receives `503`. The fourth failed
attempt moves generation 0 to `dead_lettered` under the accelerated local retry
policy.

After the terminal state is visible through the query API, the guided demo acts
as the presentation operator: it explicitly repairs Receiver Lab to return HTTP
`200`, then explicitly approves an idempotent replay. The replay API appends
generation 1; the worker sends it as a separate HTTP request and persists the
`200` result as `delivered`.

The final timeline must retain both sides of the proof:

- Generation 0 remains `dead_lettered` with four completed HTTP `503` attempts.
- Generation 1 is `delivered` with its own HTTP `200` attempt.
- The immutable event and original failure evidence were not reset, moved, or
  deleted during recovery.

The live journey is the fastest recruiter walkthrough. Read it in this order:

1. **Where it is now:** the dominant status and highlighted route node.
2. **What happened so far:** the compact generation 0 and generation 1 chips.
3. **What crossed the network:** the latest real HTTP exchange and receiver URL.
4. **Why it is credible:** EventHarbor's attempt and Receiver Lab's
   independently captured receipt have matching event IDs, delivery IDs, attempt
   numbers, and exact-body SHA-256 values.
5. **Optional engineering depth:** expand exact canonical JSON, every HTTP
   attempt, or complete PostgreSQL lineage only when needed.

Receiver Lab records that a signature header arrived; it does not currently
verify the signature, and the interface does not claim that it does.

The one-click sequence is presentation orchestration, not a production
auto-replay policy. It waits for persisted terminal evidence and then issues the
same distinct repair and explicit replay commands exposed to an operator.

### Secondary operator mode

Use operator mode when you want to control the recovery boundary yourself. The
system performs the same real publish, delivery, retry, and dead-letter work but
pauses at generation 0. Inspect the four `503` attempts, choose **Repair
receiver**, review the replay action, and choose **Approve replay**. Repairing
the receiver alone never changes the dead-lettered delivery; only the separate
replay approval creates generation 1.

You can continue exploring through the top navigation:

- **Events** lists recent events and their latest delivery state.
- **Dead letters** contains only latest generations that still need action.
- **Endpoints** shows safe Receiver Lab metadata without a signing secret.

The interface uses relative `/api` requests. Nginx proxies those requests to
FastAPI inside Compose, so the browser only communicates with
`http://localhost:3000` during the guided flow.

### Concurrent visitor isolation

Every browser run carries a safe run ID from its editable event payload to the
worker and Receiver Lab. Receiver presets, counters, and receipts are stored per
run, so concurrent visitors cannot repair or exhaust each other's scenario.
Receiver Lab bounds this in-memory demo state to 100 recently used runs and 100
receipts per run; PostgreSQL event, delivery, and attempt records remain durable
independently of that bounded presentation evidence.

## 3. Service addresses and troubleshooting

- Control Room: <http://localhost:3000>
- API health: <http://localhost:8000/health>
- API documentation: <http://localhost:8000/docs>
- Receiver Lab health: <http://localhost:8100/health>
- Receiver Lab documentation: <http://localhost:8100/docs>

Check service state with:

```powershell
docker compose ps
```

Follow API and worker output with:

```powershell
docker compose logs -f api worker
```

Stop the stack with `docker compose down`. PostgreSQL data stays in the named
volume, so events remain available on the next start.

## 4. Frontend development with hot reload

Run the backend services in one terminal:

```powershell
docker compose up postgres migrate api worker receiver-lab
```

Run Vite in a second terminal:

```powershell
cd frontend
npm ci
npm run dev
```

Open <http://localhost:5173>. Vite proxies `/api/*` to
`http://localhost:8000/*`, matching the Nginx behavior in the complete stack.

## 5. Optional API-only walkthrough

The remaining steps prove the same behavior directly through HTTP calls.

### 5.1 Configure a successful receiver

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

### 5.2 Register Receiver Lab

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

### 5.3 Publish an event

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

### 5.4 Inspect delivery evidence

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

### 5.5 Force a terminal failure

Change Receiver Lab to return a permanent HTTP `400` response. This optional
API-only walkthrough takes a shorter terminal path so the commands remain
compact; the browser's default destination-outage scenario uses four HTTP `503`
attempts to demonstrate the retry policy before dead-lettering, while the
permanent-rejection scenario exposes this same one-request classification visually.

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

### 5.6 Repair the receiver and approve replay

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

### 5.7 Prove the replay succeeded without erasing history

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
