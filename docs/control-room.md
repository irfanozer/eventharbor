# Control Room

The Control Room is the visual operator surface for EventHarbor. Its default
experience is a recruiter-first live delivery journey built around one event
instead of several disconnected dashboards:

```text
browser -> API -> PostgreSQL -> worker -> Receiver Lab
```

Every state shown in the interface comes from real FastAPI commands and queries,
PostgreSQL records, worker decisions, and HTTP exchanges with Receiver Lab. The
browser does not invent attempt results, delete the failed generation, or turn a
dead letter back into pending work. Replay appends generation 1 while generation
0 and its attempt evidence remain available.

The primary demo begins with an editable synthetic order and a selectable
receiver incident, so no real customer data is needed. Only the business payload
and chosen test condition are synthetic. The browser sends the event through the
same live API, PostgreSQL transaction, worker, and HTTP transport used by the
rest of EventHarbor. Receiver Lab runs as a separate FastAPI service and records
what reached it independently from the worker's delivery records.

## Browser routes

| Route | Purpose |
| --- | --- |
| `/` | Recruiter-first live event journey and selectable receiver incidents |
| `/events` | Filterable recent events and latest-generation status |
| `/events/:eventId` | Immutable event data, delivery generations, and attempt timelines |
| `/dead-letters` | Latest-generation deliveries requiring operator action |
| `/endpoints` | Registered destinations without signing secrets |
| `/endpoints/:endpointId` | Safe destination metadata |

Nginx returns `index.html` for these paths. React Router then chooses the page
in the browser. A refresh on `/events/{id}` therefore works even though there is
no physical HTML file at that path.

## Browser-facing API

The browser calls relative `/api/v1/*` URLs. In the container stack, Nginx
removes the `/api` prefix and forwards the request to FastAPI. During frontend
development, Vite provides the same proxy behavior.

| Method and path | Purpose |
| --- | --- |
| `GET /api/v1/control-room/overview` | Current event, endpoint, and delivery counts |
| `GET /api/v1/events` | Cursor-paginated event list with optional filters |
| `POST /api/v1/events` | Durably publish a synthetic demo event |
| `GET /api/v1/events/{eventId}` | Event and every delivery generation |
| `GET /api/v1/deliveries/{deliveryId}/attempts` | Durable attempt timeline |
| `POST /api/v1/deliveries/{deliveryId}/replays` | Create an operator-approved replay generation |
| `GET /api/v1/dead-letters` | Latest-generation actionable dead letters |
| `GET /api/v1/endpoints` | Cursor-paginated safe endpoint list |
| `POST /api/v1/endpoints` | Register the built-in Receiver Lab |
| `GET /api/v1/endpoints/{endpointId}` | Safe endpoint detail |
| `GET /api/v1/demo/receiver-lab` | Read the bounded demo receiver state |
| `PUT /api/v1/demo/receiver-lab` | Apply a server-owned failure preset |

The endpoint list and detail responses deliberately omit signing secrets. The
endpoint creation response reveals a secret once, and the Control Room neither
stores nor displays it.

## Recruiter-first event journey

The page keeps one large `NOW` status, one physical route, one compact response
history, and one latest HTTP exchange in the same stable panel. A recruiter does
not need to combine a status dashboard, timeline, and proof page to understand
where the event is. The dominant status can report, for example:

```text
POSTGRESQL · SAFE
WORKER -> RECEIVER · G0 REQUEST 2
RETRY SCHEDULER · BACKPRESSURE
DEAD LETTER · STOPPED
DELIVERED · VERIFIED
```

The fixed route is:

```text
Browser -> EventHarbor API -> PostgreSQL -> delivery worker -> Receiver Lab
```

Only the active location is highlighted. A moving request marker is shown only
while an in-progress attempt exists in durable state; animation never invents a
delivery transition.

## Selectable Receiver Lab incidents

Receiver behavior is selected before the event is published and stored in the
event data. The browser cannot send an arbitrary URL or arbitrary failure count.
Each scenario is server owned, reproducible, and isolated by run ID.

| Recruiter scenario | Actual receiver sequence | Expected EventHarbor result |
| --- | --- | --- |
| Destination outage | `503, 503, 503, 503`, repair, then G1 `200` | Dead letter plus explicit replay |
| Brief service outage | `503, 503, 200` | Generation 0 recovers automatically |
| API rate limit | `429, 429, 200`, with `Retry-After: 2` on each `429` | Worker follows receiver backpressure and generation 0 delivers |
| Invalid request | one `400` | Terminal classification; no pointless retry |

The default destination-outage action runs the complete recovery story from one
click. The presentation controller acts as the demo operator while every
underlying step still uses the normal API and persistence boundaries:

1. Select the `dead_letter` Receiver Lab preset.
2. Find or register the built-in Receiver Lab endpoint.
3. Publish a synthetic event with a new idempotency key.
4. Poll the persisted event and attempt APIs while the worker performs four
   real HTTP requests that receive `503`.
5. Wait until generation 0 is durably `dead_lettered`.
6. Repair Receiver Lab by explicitly changing it to the `success` preset.
7. Explicitly approve a replay through the replay API, appending generation 1.
8. Watch the worker send generation 1, receive HTTP `200`, and persist it as
   `delivered`.

The interface advances as PostgreSQL-backed evidence appears; it does not fake
progress with a scripted animation or depend on a fixed sleep. The final view
retains generation 0 and all four failed attempts beside the successful
generation 1 attempt.

The response history separates generation 0 from generation 1 in two compact
rows. A visible recovery boundary states that there is **no fifth automatic
retry**: the successful request is request 1 of a new delivery generation,
created only after the receiver is changed from `503` to `200` and a replay is
explicitly approved.

For each actual request, the trace correlates two independently stored views:

- EventHarbor's attempt record: delivery ID, attempt number, timestamp, duration,
  and HTTP result.
- Receiver Lab's receipt: event ID, delivery ID, attempt header, receive time,
  response status, exact-body SHA-256, body preview, and whether a signature
  header was present.

The latest exchange makes the actual destination URL, generation, request number,
timestamp, response code, duration, and independent receiver receipt visible at
once. Matching IDs and body hashes demonstrate that the receiver observed the
same request the worker says it sent. The demo deliberately says **signature
header observed**, not signature verified; cryptographic verification is not
currently performed by Receiver Lab.

Canonical JSON, all attempt fields, complete PostgreSQL lineage, and engineering
details are progressively disclosed below the journey. They remain available for
an engineering interview without competing with the first 20-second explanation.

The selected recovery actor is stored with the browser's active story. Once an
event starts, the Guided/Operator selector is locked, so a later UI toggle or
reload cannot relabel a guided replay as a human-approved operator action.

The automatic repair and replay approval are **presentation orchestration** for
the guided local demo. They do not mean that EventHarbor automatically replays
dead letters in normal operation. The controller makes a distinct repair call
and an explicit, idempotent replay command after observing the terminal state,
the same operations an operator can choose manually.

## Secondary operator mode

Operator mode leaves the recovery decision with the visitor. It performs the
same publish-and-fail setup, then pauses after generation 0 becomes
`dead_lettered`. The visitor can inspect the four persisted `503` attempts,
choose **Repair receiver**, review the replay warning, and choose **Approve
replay**. This mode is useful for a deeper engineering conversation because it
makes the boundary between repairing a destination and creating new delivery
work explicit.

With an already healthy local stack, the complete guided story usually finishes
within 15 seconds. A 90-second safety timeout protects the interface when a
service or worker is unavailable.

## Per-run Receiver Lab isolation

Each guided or operator demo creates a bounded, header-safe run ID. The Control
Room scopes receiver reads and preset changes with `?run_id=...`; the worker
copies the same identifier from the immutable event payload into the internal
`X-EventHarbor-Demo-Run-Id` header. Receiver Lab therefore keeps an independent
configuration, attempt counter, sequence, and bounded receipt history for each
active run. One visitor repairing a receiver cannot turn another visitor's
generation 0 requests into successes.

Receiver Lab keeps at most 100 scoped runs using least-recently-used eviction,
and each run retains at most 100 observations. Unscoped control calls remain as
a backward-compatible local/API testing surface and are never mixed into a
configured scoped run.

## Local and production retry policy

The Compose stack intentionally uses an accelerated policy for the interactive
demo:

| Setting | Local demo | Backend production default |
| --- | ---: | ---: |
| Maximum attempts | 4 | 8 |
| Base retry delay | 1 second | 10 seconds |
| Maximum retry delay | 2 seconds | 3,600 seconds |
| `Retry-After` cap | 2 seconds | 3,600 seconds |

This changes how quickly the story unfolds, not the delivery state machine or
the durability guarantees. Production values must be chosen from measured
destination behavior, capacity, and recovery objectives.

## Delivery shape

The frontend build is a static artifact:

```text
TypeScript + React source
       |
       v
Vite production build
       |
       v
hashed assets + index.html
       |
       v
Nginx :80
  |          |
  |          +-> /api/* -> FastAPI :8000
  +-> /* -> static file or index.html
```

The same-origin proxy keeps deployment simple and avoids enabling a wildcard
cross-origin policy on the API.
