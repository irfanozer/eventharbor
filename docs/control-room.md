# Control Room

The Control Room is the visual operator surface for EventHarbor. Its default
experience is a recruiter-first, one-click live recovery demonstration built
around one inspectable reliability story instead of a decorative dashboard:

```text
publish -> repeated 503 responses -> dead letter -> repair -> approved replay -> 200
```

Every state shown in the interface comes from real FastAPI commands and queries,
PostgreSQL records, worker decisions, and HTTP exchanges with Receiver Lab. The
browser does not invent attempt results, delete the failed generation, or turn a
dead letter back into pending work. Replay appends generation 1 while generation
0 and its attempt evidence remain available.

## Browser routes

| Route | Purpose |
| --- | --- |
| `/` | Operational overview and guided reliability story |
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

## Recruiter-first guided mode

The primary action runs the complete live recovery story from one click. The
presentation controller acts as the demo operator while every underlying step
still uses the normal API and persistence boundaries:

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

## Shared Receiver Lab state

Receiver Lab currently keeps one process-wide, in-memory configuration. In a
public deployment, two visitors running the demonstration at the same time can
change the receiver behavior for each other even though their events and
delivery evidence have different database IDs. Treat the current demo as a
single-visitor presentation. A multi-visitor public version needs per-session
Receiver Lab isolation or serialized demo sessions; the current interface does
not claim that isolation.

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
