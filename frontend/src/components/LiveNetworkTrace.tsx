import type { Delivery, DeliveryAttempt } from "../types";

export interface LiveTraceReceiverObservation {
  sequence: number;
  event_id: string | null;
  delivery_id: string | null;
  event_type: string | null;
  delivery_attempt: number | null;
  request_timestamp: number | null;
  received_at: string;
  response_status_code: number;
  receiver_mode: string;
  signature_present: boolean;
  body_sha256: string;
  body_preview: string;
}

export interface LiveNetworkTraceProps {
  demoMode: "guided" | "operator";
  runId?: string;
  eventId: string;
  eventType: string;
  eventSource: string;
  eventCreatedAt: string;
  payloadHash: string;
  payload: Record<string, unknown>;
  endpointName: string;
  endpointUrl: string;
  originalDelivery: Delivery | null;
  replayDelivery: Delivery | null;
  originalAttempts: DeliveryAttempt[];
  replayAttempts: DeliveryAttempt[];
  receiverObservations: LiveTraceReceiverObservation[];
  receiverStatus: "loading" | "unavailable" | "offline" | "online";
  repairOccurred: boolean;
  replayOccurred: boolean;
}

interface TimestampValue {
  dateTime?: string;
  label: string;
}

function timestampFrom(value: string | number | null | undefined): TimestampValue {
  if (value === null || value === undefined || value === "") {
    return { label: "Not recorded yet" };
  }

  const numericValue = typeof value === "number" && value < 1_000_000_000_000
    ? value * 1_000
    : value;
  const date = new Date(numericValue);

  if (Number.isNaN(date.getTime())) {
    return { label: String(value) };
  }

  const iso = date.toISOString();
  return {
    dateTime: iso,
    label: iso.replace("T", " ").replace("Z", " UTC"),
  };
}

function Timestamp({ value }: { value: string | number | null | undefined }) {
  const timestamp = timestampFrom(value);
  if (!timestamp.dateTime) return <span>{timestamp.label}</span>;
  return <time dateTime={timestamp.dateTime}>{timestamp.label}</time>;
}

function attemptOutcome(attempt: DeliveryAttempt): string {
  if (attempt.http_status_code !== null) return `HTTP ${attempt.http_status_code}`;
  if (attempt.error_type) return attempt.error_type;
  if (attempt.status === "in_progress") return "Request in flight";
  return "No HTTP response";
}

function attemptTimestamp(attempt: DeliveryAttempt): string | number | null {
  return attempt.request_timestamp ?? attempt.started_at ?? attempt.created_at;
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }

  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      // Match Python json.dumps(sort_keys=True) for the ASCII schema keys used
      // by the wire envelope; locale-aware sorting is not byte-deterministic.
      .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
      .map(([key, entryValue]) => `${JSON.stringify(key)}:${canonicalJson(entryValue)}`);
    return `{${entries.join(",")}}`;
  }

  return JSON.stringify(value);
}

function receiverBodyMatches(payload: Record<string, unknown>, bodyPreview: string): boolean {
  try {
    return canonicalJson(payload) === canonicalJson(JSON.parse(bodyPreview));
  } catch {
    return false;
  }
}

function matchingObservation(
  observations: LiveTraceReceiverObservation[],
  delivery: Delivery | null,
  attempt: DeliveryAttempt,
): LiveTraceReceiverObservation | undefined {
  return observations.find(
    (observation) =>
      observation.delivery_id === delivery?.id &&
      observation.delivery_attempt === attempt.attempt_number,
  );
}

interface AttemptLaneProps {
  generation: 0 | 1;
  title: string;
  description: string;
  delivery: Delivery | null;
  attempts: DeliveryAttempt[];
  endpointUrl: string;
  receiverObservations: LiveTraceReceiverObservation[];
}

function AttemptLane({
  generation,
  title,
  description,
  delivery,
  attempts,
  endpointUrl,
  receiverObservations,
}: AttemptLaneProps) {
  const orderedAttempts = [...attempts].sort(
    (left, right) => left.attempt_number - right.attempt_number,
  );

  return (
    <article
      className={`live-trace-lane live-trace-lane--generation-${generation}`}
      aria-labelledby={`live-trace-generation-${generation}-label live-trace-generation-${generation}-title`}
    >
      <header className="live-trace-lane-header">
        <div>
          <p className="live-trace-eyebrow" id={`live-trace-generation-${generation}-label`}>
            Generation {generation}
          </p>
          <h3 id={`live-trace-generation-${generation}-title`}>{title}</h3>
          <p>{description}</p>
        </div>
        <span className="live-trace-delivery-status" data-status={delivery?.status ?? "not-created"}>
          {delivery?.status.replaceAll("_", " ") ?? "Not created"}
        </span>
      </header>

      {orderedAttempts.length > 0 ? (
        <ol className="live-trace-attempts" aria-label={`Generation ${generation} HTTP requests`}>
          {orderedAttempts.map((attempt) => {
            const observation = matchingObservation(receiverObservations, delivery, attempt);
            return (
              <li className="live-trace-attempt" key={attempt.id}>
                <div className="live-trace-attempt-index">
                  <span>{generation === 0 ? "Automatic attempt" : "Replay request"}</span>
                  <strong>#{attempt.attempt_number}</strong>
                </div>

                <dl className="live-trace-attempt-facts">
                  <div>
                    <dt>Request</dt>
                    <dd>POST</dd>
                  </div>
                  <div>
                    <dt>Destination</dt>
                    <dd><code>{endpointUrl}</code></dd>
                  </div>
                  <div>
                    <dt>Sent at</dt>
                    <dd><Timestamp value={attemptTimestamp(attempt)} /></dd>
                  </div>
                  <div>
                    <dt>Response</dt>
                    <dd>{attemptOutcome(attempt)}</dd>
                  </div>
                  <div>
                    <dt>Duration</dt>
                    <dd>{attempt.duration_ms === null ? "Not recorded yet" : `${attempt.duration_ms} ms`}</dd>
                  </div>
                  <div>
                    <dt>Receiver receipt</dt>
                    <dd>
                      {observation ? (
                        <>#{observation.sequence} at <Timestamp value={observation.received_at} /></>
                      ) : (
                        "Awaiting receiver observation"
                      )}
                    </dd>
                  </div>
                </dl>

                <p className="live-trace-attempt-mobile-summary">
                  <strong>{attemptOutcome(attempt)}</strong>
                  <span><Timestamp value={attemptTimestamp(attempt)} /></span>
                  <span>{attempt.duration_ms === null ? "Duration pending" : `${attempt.duration_ms} ms`}</span>
                  <span>{observation ? `Receiver receipt #${observation.sequence}` : "Awaiting receiver receipt"}</span>
                </p>

                {attempt.response_body_excerpt ? (
                  <p className="live-trace-response-body">
                    <span>Response body</span> <code>{attempt.response_body_excerpt}</code>
                  </p>
                ) : null}
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="live-trace-empty-state">
          {generation === 0
            ? "The event is stored. The worker has not made an HTTP request yet."
            : "No replay delivery exists yet."}
        </p>
      )}
    </article>
  );
}

export function LiveNetworkTrace({
  demoMode,
  runId,
  eventId,
  eventType,
  eventSource,
  eventCreatedAt,
  payloadHash,
  payload,
  endpointName,
  endpointUrl,
  originalDelivery,
  replayDelivery,
  originalAttempts,
  replayAttempts,
  receiverObservations,
  receiverStatus,
  repairOccurred,
  replayOccurred,
}: LiveNetworkTraceProps) {
  const matchingObservations = receiverObservations
    .filter((observation) => observation.event_id === eventId)
    .sort((left, right) => left.sequence - right.sequence);
  const latestReceiverObservation = matchingObservations.at(-1);
  const successfulReplayReceipt = replayDelivery?.status === "delivered"
    ? matchingObservations.find((observation) =>
        observation.delivery_id === replayDelivery.id &&
        observation.response_status_code >= 200 &&
        observation.response_status_code < 300,
      )
    : undefined;
  const displayedReceipt = successfulReplayReceipt ?? latestReceiverObservation;
  const bodyMatches = displayedReceipt
    ? receiverBodyMatches(payload, displayedReceipt.body_preview)
    : false;
  const hashMatches = displayedReceipt?.body_sha256 === payloadHash;
  const successfulReceiptVerified = Boolean(
    successfulReplayReceipt &&
    successfulReplayReceipt.event_id === eventId &&
    bodyMatches &&
    hashMatches,
  );
  const recoveryActor = demoMode === "guided" ? "Guided demo" : "Operator";
  const wireBody = canonicalJson(payload);
  const receiverStatusLabel = {
    loading: "Checking receiver state…",
    unavailable: "Receiver state unavailable",
    offline: "HTTP 503 offline",
    online: "HTTP 200 online",
  }[receiverStatus];
  const repairPath = `/api/v1/demo/receiver-lab${runId ? `?run_id=${encodeURIComponent(runId)}` : ""}`;
  const replayPath = originalDelivery
    ? `/api/v1/deliveries/${originalDelivery.id}/replays`
    : "/api/v1/deliveries/{delivery_id}/replays";

  return (
    <section className="live-trace" id="live-network-trace" aria-labelledby="live-trace-title">
      <header className="live-trace-intro">
        <p className="live-trace-eyebrow">Live network trace</p>
        <h2 id="live-trace-title">One payload crosses three real runtime boundaries.</h2>
        <p>
          Follow the exact event from this browser, through durable storage and a background
          worker, to a separate HTTP receiver.
        </p>
      </header>

      <ol className="live-trace-route" aria-label="Actual event route">
        <li className="live-trace-route-node live-trace-route-node--origin">
          <p className="live-trace-node-index">01 / From</p>
          <h3>Browser demo client</h3>
          <p>This browser published <code>{eventType}</code>. The API recorded source <strong>{eventSource}</strong>.</p>
          <p><code>POST /api/v1/events</code></p>
          <dl>
            <div><dt>Event ID</dt><dd><code>{eventId}</code></dd></div>
            <div><dt>Created</dt><dd><Timestamp value={eventCreatedAt} /></dd></div>
          </dl>
        </li>

        <li className="live-trace-route-node live-trace-route-node--harbor">
          <p className="live-trace-node-index">02 / Protected by</p>
          <h3>EventHarbor</h3>
          <p>API accepts → PostgreSQL stores → worker delivers.</p>
          <p className="live-trace-node-proof">Stored before the first outbound HTTP request.</p>
        </li>

        <li className="live-trace-route-node live-trace-route-node--receiver">
          <p className="live-trace-node-index">03 / To</p>
          <h3>{endpointName}</h3>
          <p>Separate Receiver Lab FastAPI service.</p>
          <p><code>{endpointUrl}</code></p>
          <dl>
            <div>
              <dt>Current response</dt>
              <dd>{receiverStatusLabel}</dd>
            </div>
          </dl>
        </li>
      </ol>

      <section className="live-trace-payload" aria-labelledby="live-trace-payload-title">
        <header>
          <div>
            <p className="live-trace-eyebrow">Exact request body</p>
            <h3 id="live-trace-payload-title">These canonical bytes must arrive at the destination.</h3>
          </div>
          <dl className="live-trace-payload-identity">
            <div><dt>Event</dt><dd><code>{eventId}</code></dd></div>
            <div><dt>SHA-256</dt><dd><code>{payloadHash}</code></dd></div>
          </dl>
        </header>
        <pre><code>{wireBody}</code></pre>
      </section>

      <div className="live-trace-generations">
        <AttemptLane
          generation={0}
          title="Automatic delivery"
          description="The worker applies the configured retry budget while the receiver returns HTTP 503."
          delivery={originalDelivery}
          attempts={originalAttempts}
          endpointUrl={endpointUrl}
          receiverObservations={matchingObservations}
        />

        <section className="live-trace-recovery-boundary" aria-label={`${recoveryActor} recovery boundary`} role="status">
          <p className="live-trace-eyebrow">Hard recovery boundary</p>
          <h3>No fifth automatic retry.</h3>
          <ol>
            <li data-complete={originalDelivery?.status === "dead_lettered"}>
              Generation 0 stopped after its bounded retry budget.
            </li>
            <li data-complete={repairOccurred}>
              {repairOccurred
                ? <>{recoveryActor} sent <code>PUT {repairPath}</code> and changed this run from HTTP 503 to HTTP 200.</>
                : `${recoveryActor} has not sent the receiver repair request yet.`}
            </li>
            <li data-complete={replayOccurred}>
              {replayOccurred
                ? <>Replay POST accepted at <code>{replayPath}</code>.</>
                : <>No replay POST has been accepted at <code>{replayPath}</code>.</>}
              {replayDelivery ? <> Generation 1 was accepted at <Timestamp value={replayDelivery.created_at} />.</> : null}
            </li>
          </ol>
          <p>
            Generation 1 is a deliberate new delivery. Its first request is not attempt five of
            generation 0.
          </p>
        </section>

        <AttemptLane
          generation={1}
          title={demoMode === "guided" ? "Guided demo replay" : "Operator-approved replay"}
          description={`${recoveryActor} appends a new generation without changing the four original failures.`}
          delivery={replayDelivery}
          attempts={replayAttempts}
          endpointUrl={endpointUrl}
          receiverObservations={matchingObservations}
        />
      </div>

      <section className="live-trace-receipt" data-verified={successfulReceiptVerified} aria-labelledby="live-trace-receipt-title">
        <header className="live-trace-receipt-header">
          <p className="live-trace-eyebrow">{successfulReceiptVerified ? "Two-sided successful delivery receipt" : "Receiver-side network evidence"}</p>
          <h3 id="live-trace-receipt-title">
            {successfulReceiptVerified
              ? "The successful replay delivered the exact stored payload."
              : displayedReceipt
                ? `Receiver Lab independently captured this request and returned HTTP ${displayedReceipt.response_status_code}.`
                : "Waiting for the first independently observed request."}
          </h3>
        </header>

        <div className="live-trace-receipt-sides">
          <article className="live-trace-receipt-side live-trace-receipt-side--sent">
            <h4>Sent by EventHarbor</h4>
            <dl>
              <div><dt>Event ID</dt><dd><code>{eventId}</code></dd></div>
              <div><dt>Body SHA-256</dt><dd><code>{payloadHash}</code></dd></div>
              <div><dt>Destination</dt><dd><code>{endpointUrl}</code></dd></div>
            </dl>
            <pre><code>{wireBody}</code></pre>
          </article>

          <article className="live-trace-receipt-side live-trace-receipt-side--received">
            <h4>Observed by Receiver Lab</h4>
            {displayedReceipt ? (
              <>
                <dl>
                  <div><dt>Event ID</dt><dd><code>{displayedReceipt.event_id ?? "Missing"}</code></dd></div>
                  <div><dt>Delivery ID</dt><dd><code>{displayedReceipt.delivery_id ?? "Missing"}</code></dd></div>
                  <div><dt>Body SHA-256</dt><dd><code>{displayedReceipt.body_sha256}</code></dd></div>
                  <div><dt>Received</dt><dd><Timestamp value={displayedReceipt.received_at} /></dd></div>
                  <div><dt>Response</dt><dd>HTTP {displayedReceipt.response_status_code}</dd></div>
                  <div><dt>Signature header observed</dt><dd>{displayedReceipt.signature_present ? "Yes" : "No"}</dd></div>
                </dl>
                <pre><code>{displayedReceipt.body_preview}</code></pre>
              </>
            ) : (
              <p className="live-trace-empty-state">No current-event request has reached Receiver Lab yet.</p>
            )}
          </article>
        </div>

        <dl className="live-trace-match-summary" data-verified={successfulReceiptVerified}>
          <div><dt>Event identity</dt><dd>{displayedReceipt?.event_id === eventId ? "Exact match" : "Waiting"}</dd></div>
          <div><dt>JSON body</dt><dd>{bodyMatches ? "Exact match" : "Waiting"}</dd></div>
          <div><dt>SHA-256</dt><dd>{hashMatches ? "Exact match" : "Waiting"}</dd></div>
          <div><dt>Delivery result</dt><dd>{successfulReceiptVerified ? "HTTP 200 verified" : displayedReceipt ? `HTTP ${displayedReceipt.response_status_code} observed` : "Waiting"}</dd></div>
        </dl>
      </section>
    </section>
  );
}
