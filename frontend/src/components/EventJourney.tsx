import { Fragment, type ReactNode } from "react";

import type { DemoScenario } from "../demoScenarios";
import { demoEventDefinition, isExpectedSchemaRejection } from "../demoEvents";
import type { ReliabilityTourPhase } from "../reliabilityTour";
import type {
  Delivery,
  DeliveryAttempt,
  EventDetail,
  ReceiverLabRequest,
} from "../types";

type JourneyLocation = "browser" | "api" | "postgres" | "worker" | "network" | "receiver";
type JourneyTone = "idle" | "active" | "warning" | "danger" | "success" | "terminal";

interface JourneyView {
  location: JourneyLocation;
  tone: JourneyTone;
  label: string;
  headline: string;
  explanation: string;
}

export interface EventJourneyProps {
  scenario: DemoScenario;
  demoMode: "guided" | "operator";
  tourPhase: ReliabilityTourPhase | null;
  tourMessage: string;
  isStarting: boolean;
  runId?: string;
  event: EventDetail | null;
  endpointName: string;
  endpointUrl: string;
  originalDelivery: Delivery | null;
  replayDelivery: Delivery | null;
  originalAttempts: DeliveryAttempt[];
  replayAttempts: DeliveryAttempt[];
  receiverObservations: ReceiverLabRequest[];
  receiverControlStatus: "idle" | "loading" | "unavailable" | "ready";
  repairOccurred: boolean;
  replayOccurred: boolean;
  maxAttempts: number;
  children?: ReactNode;
}

interface Exchange {
  generation: 0 | 1;
  delivery: Delivery;
  attempt: DeliveryAttempt;
  observation?: ReceiverLabRequest;
}

interface TimestampValue {
  dateTime?: string;
  label: string;
}

function timestampFrom(value: string | number | null | undefined): TimestampValue {
  if (value === null || value === undefined || value === "") return { label: "Not recorded yet" };
  const numericValue = typeof value === "number" && value < 1_000_000_000_000 ? value * 1_000 : value;
  const date = new Date(numericValue);
  if (Number.isNaN(date.getTime())) return { label: String(value) };
  const iso = date.toISOString();
  return { dateTime: iso, label: iso.replace("T", " ").replace("Z", " UTC") };
}

function Timestamp({ value }: { value: string | number | null | undefined }) {
  const timestamp = timestampFrom(value);
  return timestamp.dateTime
    ? <time dateTime={timestamp.dateTime}>{timestamp.label}</time>
    : <span>{timestamp.label}</span>;
}

function shortId(value: string): string {
  return value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

function deliveryName(generation: 0 | 1): "Original delivery" | "Recovery replay" {
  return generation === 0 ? "Original delivery" : "Recovery replay";
}

function deliveryRequestName(generation: 0 | 1, requestNumber: number): string {
  return `${generation === 0 ? "Original" : "Replay"} request ${requestNumber}`;
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0)
      .map(([key, entryValue]) => `${JSON.stringify(key)}:${canonicalJson(entryValue)}`);
    return `{${entries.join(",")}}`;
  }
  return JSON.stringify(value);
}

function responseName(status: number): string {
  if (status === 200) return "Accepted";
  if (status === 400) return "Bad Request";
  if (status === 429) return "Rate Limited";
  if (status === 503) return "Service Unavailable";
  return status >= 200 && status < 300 ? "Successful" : "HTTP response";
}

function routePath(url: string): string {
  try {
    return new URL(url).pathname;
  } catch {
    return "/webhooks";
  }
}

function attemptOutcome(attempt: DeliveryAttempt): string {
  if (attempt.http_status_code !== null) return `HTTP ${attempt.http_status_code}`;
  if (attempt.error_type) return attempt.error_type.replaceAll("_", " ").toUpperCase();
  if (attempt.status === "in_progress") return "IN FLIGHT";
  return "NO RESPONSE";
}

function responseBodyDetail(attempt: DeliveryAttempt): string | null {
  const excerpt = attempt.response_body_excerpt?.trim();
  if (!excerpt) return null;
  try {
    const parsed = JSON.parse(excerpt) as unknown;
    if (parsed && typeof parsed === "object") {
      const detail = (parsed as Record<string, unknown>).detail;
      if (typeof detail === "string" && detail.trim()) return detail;
    }
  } catch {
    // A plain-text receiver response is already a useful detail.
  }
  return excerpt;
}

function attemptDecision(exchange: Exchange): { label: string; detail: string } {
  const { attempt, delivery } = exchange;
  const responseDetail = responseBodyDetail(attempt);
  if (attempt.status === "in_progress") {
    return { label: "Request in flight", detail: "Waiting for Receiver Lab" };
  }
  if (attempt.http_status_code !== null && attempt.http_status_code >= 200 && attempt.http_status_code < 300) {
    return { label: "Delivered", detail: responseDetail ?? "Receiver accepted the webhook" };
  }
  if (attempt.disposition === "terminal_failure") {
    return { label: "Stopped · not retryable", detail: responseDetail ?? "Retrying cannot change the request body" };
  }
  if (delivery.status === "dead_lettered" && attempt.attempt_number === delivery.attempt_count) {
    return { label: "Retry budget exhausted", detail: responseDetail ?? "No further automatic request" };
  }
  if (attempt.http_status_code === 429) {
    return {
      label: "Retry scheduled",
      detail: attempt.retry_scheduled_for
        ? `Next try: ${timestampFrom(attempt.retry_scheduled_for).label}`
        : responseDetail ?? "Receiver requested a slower delivery rate",
    };
  }
  if (attempt.disposition === "retry") {
    return { label: "Retry scheduled", detail: responseDetail ?? "Temporary failure" };
  }
  return { label: "Recorded", detail: responseDetail ?? "No additional response detail" };
}

function responseTone(status: number | null | undefined): JourneyTone {
  if (status === undefined || status === null) return "active";
  if (status >= 200 && status < 300) return "success";
  if (status === 400) return "terminal";
  if (status === 429) return "warning";
  return "danger";
}

function matchesObservation(
  observations: ReceiverLabRequest[],
  delivery: Delivery,
  attempt: DeliveryAttempt,
): ReceiverLabRequest | undefined {
  return observations.find((observation) =>
    observation.delivery_id === delivery.id &&
    observation.delivery_attempt === attempt.attempt_number
  );
}

function buildExchanges(
  originalDelivery: Delivery | null,
  replayDelivery: Delivery | null,
  originalAttempts: DeliveryAttempt[],
  replayAttempts: DeliveryAttempt[],
  observations: ReceiverLabRequest[],
): Exchange[] {
  const exchanges: Exchange[] = [];
  if (originalDelivery) {
    for (const attempt of originalAttempts) {
      exchanges.push({
        generation: 0,
        delivery: originalDelivery,
        attempt,
        observation: matchesObservation(observations, originalDelivery, attempt),
      });
    }
  }
  if (replayDelivery) {
    for (const attempt of replayAttempts) {
      exchanges.push({
        generation: 1,
        delivery: replayDelivery,
        attempt,
        observation: matchesObservation(observations, replayDelivery, attempt),
      });
    }
  }
  return exchanges.sort((left, right) => {
    if (left.generation !== right.generation) return left.generation - right.generation;
    return left.attempt.attempt_number - right.attempt.attempt_number;
  });
}

function journeyView(
  scenario: DemoScenario,
  tourPhase: ReliabilityTourPhase | null,
  isStarting: boolean,
  event: EventDetail | null,
  originalDelivery: Delivery | null,
  replayDelivery: Delivery | null,
  latestExchange: Exchange | undefined,
  repairOccurred: boolean,
  maxAttempts: number,
  schemaRejectionVerified: boolean,
): JourneyView {
  if (!event) {
    if (isStarting || tourPhase === "preparing") {
      return {
        location: "api",
        tone: "active",
        label: "API · ACCEPTING",
        headline: "The browser is publishing your business event.",
        explanation: "EventHarbor is preparing the isolated receiver behavior and accepting POST /api/v1/events.",
      };
    }
    return {
      location: "browser",
      tone: "idle",
      label: "READY · BROWSER",
      headline: "Ready to send one real business event.",
      explanation: "Choose an incident above. Every status below will come from an API, database row, worker attempt, or receiver receipt.",
    };
  }

  if (tourPhase === "paused" || tourPhase === "failed") {
    return {
      location: originalDelivery?.status === "dead_lettered" ? "worker" : "postgres",
      tone: tourPhase === "failed" ? "danger" : "warning",
      label: tourPhase === "failed" ? "DEMO · NEEDS ATTENTION" : "PAUSED · DURABLE",
      headline: tourPhase === "failed" ? "The guided demonstration stopped." : "Paused at a durable state.",
      explanation: "The event and all network evidence already created remain stored and can be resumed.",
    };
  }

  if (tourPhase === "repairing" || (repairOccurred && !replayDelivery && scenario.strategy === "replay")) {
    return {
      location: "receiver",
      tone: "active",
      label: "RECEIVER LAB · HEALTHY",
      headline: "The test receiver is accepting requests again.",
      explanation: "The receiver control response confirmed HTTP 200 mode instead of HTTP 503. The original event remains stopped and saved until a separate, traceable replay is created.",
    };
  }

  const currentDelivery = replayDelivery ?? originalDelivery;
  if (currentDelivery?.status === "delivered") {
    if (replayDelivery) {
      return {
        location: "receiver",
        tone: "success",
        label: "DELIVERED · VERIFIED",
        headline: "The recovery replay reached Receiver Lab.",
        explanation: `All ${maxAttempts} failed original requests remain preserved. Replay request 1 received HTTP 200 and is separate from the automatic retry sequence.`,
      };
    }
    return {
      location: "receiver",
      tone: "success",
      label: "DELIVERED · AUTOMATIC RECOVERY",
      headline: scenario.id === "rate_limit_recovery"
        ? "Delivered after respecting the receiver's rate limit."
        : "Delivered after the destination recovered.",
      explanation: `The original delivery completed in ${currentDelivery.attempt_count} real HTTP requests. No manual intervention or replay was needed.`,
    };
  }

  if (originalDelivery?.status === "dead_lettered") {
    if (scenario.strategy === "terminal") {
      const invalidField = demoEventDefinition(event.type).invalidField;
      if (!schemaRejectionVerified) {
        return {
          location: "worker",
          tone: "danger",
          label: "STOPPED · EVIDENCE NEEDS REVIEW",
          headline: "The delivery stopped, but not with the expected schema proof.",
          explanation: `The demo expected one terminal HTTP 400 with response code missing_${invalidField}. Open every HTTP attempt to inspect the actual persisted result.`,
        };
      }
      return {
        location: "worker",
        tone: "terminal",
        label: "STOPPED AND SAVED · CORRECTLY CLASSIFIED",
        headline: `HTTP 400: data.${invalidField} was missing.`,
        explanation: `Receiver Lab is online, but its ${event.type} contract requires data.${invalidField}. The unchanged body would fail again, so EventHarbor saved the evidence and correctly made no pointless retry.`,
      };
    }
    return {
      location: "worker",
      tone: "danger",
      label: "AUTOMATIC DELIVERY STOPPED · EVENT SAVED",
      headline: `Retry limit reached after ${originalDelivery.attempt_count} failed requests.`,
      explanation: `No automatic request ${maxAttempts + 1} will occur. EventHarbor calls this dead-lettered: sending has stopped, but the event and every failure remain stored for a safe, approved replay.`,
    };
  }

  if (tourPhase === "replaying" && !replayDelivery) {
    return {
      location: "postgres",
      tone: "active",
      label: "REPLAY · SAVING NEW DELIVERY",
      headline: "A separate recovery replay is being saved.",
      explanation: "The original failed requests stay unchanged. This replay will begin again at request 1.",
    };
  }

  if (currentDelivery?.status === "in_progress" || latestExchange?.attempt.status === "in_progress") {
    const generation = replayDelivery ? 1 : 0;
    const attemptNumber = latestExchange?.attempt.attempt_number ?? currentDelivery?.attempt_count ?? 1;
    return {
      location: "network",
      tone: "active",
      label: `LIVE · ${deliveryRequestName(generation, attemptNumber).toUpperCase()}`,
      headline: "A real HTTP POST is crossing to Receiver Lab.",
      explanation: "The worker reserved this request in PostgreSQL before performing network I/O.",
    };
  }

  if (currentDelivery?.status === "retry_wait" && latestExchange) {
    const code = latestExchange.observation?.response_status_code ?? latestExchange.attempt.http_status_code;
    return {
      location: "worker",
      tone: responseTone(code),
      label: code === 429 ? "RETRY SCHEDULER · BACKPRESSURE" : "RETRY SCHEDULER · WAITING",
      headline: code ? `Receiver Lab returned HTTP ${code} ${responseName(code)}.` : "The last request did not return a response.",
      explanation: code === 429
        ? "EventHarbor stored the response and follows Retry-After before the next request."
        : "The event remains safe in PostgreSQL while the bounded retry delay runs.",
    };
  }

  return {
    location: "postgres",
    tone: "active",
    label: "POSTGRESQL · SAFE",
    headline: "Saved before delivery.",
    explanation: `EventHarbor returned HTTP 202 for ${shortId(event.id)}. The ${replayDelivery ? "recovery replay" : "original delivery"} is stored and waiting for the worker.`,
  };
}

function nodeState(
  node: Exclude<JourneyLocation, "network">,
  location: JourneyLocation,
  event: EventDetail | null,
  delivered: boolean,
): "pending" | "complete" | "active" {
  if (location === node) return "active";
  if (!event) return node === "browser" && location !== "browser" ? "complete" : "pending";
  const order: Array<Exclude<JourneyLocation, "network">> = ["browser", "api", "postgres", "worker", "receiver"];
  if (location === "network") return order.indexOf(node) <= order.indexOf("worker") ? "complete" : "pending";
  const activeIndex = order.indexOf(location);
  const nodeIndex = order.indexOf(node);
  if (delivered || nodeIndex < activeIndex) return "complete";
  return "pending";
}

function historyStatus(delivery: Delivery | null): string {
  if (!delivery) return "NOT CREATED";
  if (delivery.status === "dead_lettered") return "STOPPED · SAVED";
  return delivery.status.replaceAll("_", " ").toUpperCase();
}

function AttemptHistory({
  generation,
  delivery,
  attempts,
}: {
  generation: 0 | 1;
  delivery: Delivery | null;
  attempts: DeliveryAttempt[];
}) {
  const ordered = [...attempts].sort((left, right) => left.attempt_number - right.attempt_number);
  return (
    <div className="journey-history-row" aria-label={`${deliveryName(generation)} actual request history`}>
      <div className="journey-history-label">
        <strong>{generation === 0 ? "ORIGINAL" : "REPLAY"}</strong>
        <span>{generation === 0 ? "First delivery" : "Separate recovery delivery"}</span>
      </div>
      <div className="journey-history-chips">
        {ordered.length ? ordered.map((attempt) => (
          <span
            className="journey-history-chip"
            data-tone={responseTone(attempt.http_status_code)}
            key={attempt.id}
            title={timestampFrom(attempt.request_timestamp ?? attempt.started_at ?? attempt.created_at).label}
          >
            <small>#{attempt.attempt_number}</small>
            <strong>{attemptOutcome(attempt)}</strong>
          </span>
        )) : <span className="journey-history-empty">Waiting for the first request</span>}
      </div>
      <span className="journey-history-state" data-status={delivery?.status ?? "pending"}>{historyStatus(delivery)}</span>
    </div>
  );
}

export function EventJourney({
  scenario,
  demoMode,
  tourPhase,
  tourMessage,
  isStarting,
  runId,
  event,
  endpointName,
  endpointUrl,
  originalDelivery,
  replayDelivery,
  originalAttempts,
  replayAttempts,
  receiverObservations,
  receiverControlStatus,
  repairOccurred,
  replayOccurred,
  maxAttempts,
  children,
}: EventJourneyProps) {
  const currentEventObservations = event
    ? receiverObservations.filter((observation) => observation.event_id === event.id)
    : [];
  const exchanges = buildExchanges(
    originalDelivery,
    replayDelivery,
    originalAttempts,
    replayAttempts,
    currentEventObservations,
  );
  const latestExchange = exchanges.at(-1);
  const latestObservation = [...currentEventObservations].sort(
    (left, right) => left.sequence - right.sequence,
  ).at(-1);
  const exchangeResponse = latestExchange?.observation?.response_status_code
    ?? latestExchange?.attempt.http_status_code
    ?? null;
  const latestResponseDetail = latestExchange ? responseBodyDetail(latestExchange.attempt) : null;
  const currentResponse = latestObservation?.response_status_code
    ?? latestExchange?.attempt.http_status_code
    ?? null;
  const currentDelivery = replayDelivery ?? originalDelivery;
  const delivered = currentDelivery?.status === "delivered";
  const schemaRejectionVerified = isExpectedSchemaRejection(
    event?.type,
    originalDelivery,
    originalAttempts,
  );
  const completed = delivered || (
    scenario.strategy === "terminal" && schemaRejectionVerified
  );
  const view = journeyView(
    scenario,
    tourPhase,
    isStarting,
    event,
    originalDelivery,
    replayDelivery,
    latestExchange,
    repairOccurred,
    maxAttempts,
    schemaRejectionVerified,
  );
  const wirePayload = event ? {
    id: event.id,
    type: event.type,
    created_at: event.created_at,
    data: event.data,
  } : null;
  const wireBody = wirePayload ? canonicalJson(wirePayload) : null;
  const successfulObservation = replayDelivery
    ? currentEventObservations.find((observation) =>
        observation.delivery_id === replayDelivery.id &&
        observation.response_status_code >= 200 &&
        observation.response_status_code < 300
      )
    : currentEventObservations.find((observation) =>
        observation.delivery_id === originalDelivery?.id &&
        observation.response_status_code >= 200 &&
        observation.response_status_code < 300
      );
  const proofObservation = successfulObservation ?? latestObservation;
  const hashMatches = Boolean(event && proofObservation?.body_sha256 === event.payload_sha256);
  const activeGeneration = latestExchange?.generation ?? (replayDelivery ? 1 : 0);
  const workerStatus = currentDelivery?.status === "dead_lettered"
    ? "STOPPED · EVENT SAVED"
    : latestExchange
      ? deliveryRequestName(activeGeneration, latestExchange.attempt.attempt_number).toUpperCase()
      : event
        ? "WAITING FOR WORK"
        : "NOT STARTED";
  const receiverNodeStatus = receiverControlStatus === "unavailable"
    ? "CONTROL UNAVAILABLE"
    : currentResponse
      ? `LAST HTTP ${currentResponse} · ${responseName(currentResponse)}`
      : receiverControlStatus === "loading"
        ? "CHECKING RECEIVER"
        : `ARMED · FIRST RESPONSE ${scenario.sequence[0]}`;
  const actor = demoMode === "guided" ? "Guided demo" : "Operator";
  const routeNodes = [
    {
      node: "browser",
      index: "01",
      label: "Browser UI",
      address: "This page",
      status: event ? "EVENT SENT" : "READY TO SEND",
      operation: "START · THIS BROWSER",
    },
    {
      node: "api",
      index: "02",
      label: "EventHarbor API",
      address: "Browser route · /api/v1/events",
      status: event ? "HTTP 202 · ACCEPTED" : "WAITING",
      operation: "HTTP INTAKE · POST /api/v1/events",
    },
    {
      node: "postgres",
      index: "03",
      label: "PostgreSQL",
      address: "Docker service · postgres:5432",
      status: event ? "EVENT STORED · SAFE" : "WAITING",
      operation: "ATOMIC STORE · SQL TRANSACTION",
    },
    {
      node: "worker",
      index: "04",
      label: "Delivery worker",
      address: "Background process · no public port",
      status: workerStatus,
      operation: "BACKGROUND CLAIM · STORED DELIVERY",
    },
    {
      node: "receiver",
      index: "05",
      label: endpointName,
      address: `Worker target · ${endpointUrl}`,
      status: receiverNodeStatus,
      operation: `OUTBOUND WEBHOOK · POST ${routePath(endpointUrl)}`,
    },
  ] as const;
  const currentRouteDescription = view.location === "network"
    ? "Current location: outbound HTTP request from the delivery worker to the receiver."
    : `Current location: ${routeNodes.find((route) => route.node === view.location)?.label ?? "EventHarbor"}.`;

  return (
    <section className="event-journey" id="live-proof" data-tone={view.tone} aria-labelledby="event-journey-title">
      <header className="journey-now" aria-live="polite">
        <div className="journey-now-badge"><span>NOW</span><strong>{view.label}</strong></div>
        <div>
          <p className="journey-eyebrow">One event · one continuous live story</p>
          <h2 id="event-journey-title">{view.headline}</h2>
          <p>{view.explanation}</p>
        </div>
        {event ? (
          <div className="journey-event-identity">
            <span>Following</span>
            <strong>{shortId(event.id)}</strong>
            <small>{scenario.shortLabel}</small>
          </div>
        ) : null}
      </header>

      <p className="sr-only" id="journey-route-current">{currentRouteDescription}</p>
      <ol className="journey-route" data-location={view.location} aria-label="Live event location" aria-describedby="journey-route-current">
        {routeNodes.map(({ node, index, label, address, status, operation }, routeIndex) => {
          const state = nodeState(node, view.location, event, delivered);
          return (
            <Fragment key={node}>
              <li
                className="journey-route-node"
                data-state={state}
                aria-current={state === "active" ? "step" : undefined}
              >
                <span className="journey-route-operation">{operation}</span>
                <span className="journey-route-index" aria-hidden="true">{index}</span>
                <strong>{label}</strong>
                <code className="journey-route-address">{address}</code>
                <small>{status}</small>
              </li>
              {routeIndex < routeNodes.length - 1 ? (
                <li
                  className="journey-route-hop"
                  data-active={view.location === "network" && node === "worker"}
                  aria-hidden="true"
                >
                  <i className="journey-connector"><b /></i>
                </li>
              ) : null}
            </Fragment>
          );
        })}
      </ol>

      <div className="journey-story-grid">
        <section className="journey-lineage" aria-labelledby="journey-lineage-title">
          <header>
            <div>
              <p className="journey-eyebrow">Actual response history</p>
              <h3 id="journey-lineage-title">Every request, in one glance.</h3>
            </div>
            <span>Read from PostgreSQL attempt rows</span>
          </header>
          <AttemptHistory generation={0} delivery={originalDelivery} attempts={originalAttempts} />
          {originalDelivery?.status === "dead_lettered" ? (
            <aside className="journey-dead-letter-explainer">
              <strong>Stopped and saved <span>(database status: dead_lettered)</span></strong>
              <p>{scenario.strategy === "terminal"
                ? "“Dead-lettered” does not mean deleted. EventHarbor preserved the event and its rejection evidence, but an unchanged replay is blocked; publish a new event with corrected data."
                : "“Dead-lettered” does not mean deleted. EventHarbor ended automatic sending, preserved the event and every HTTP attempt, and now requires review before a recovery delivery is created."}</p>
            </aside>
          ) : null}
          {scenario.strategy === "replay" && (originalDelivery?.status === "dead_lettered" || repairOccurred || replayOccurred) ? (
            <div className="journey-recovery-boundary" data-complete={Boolean(replayDelivery)}>
              <strong>Why there are two delivery rows</strong>
              <span>{repairOccurred ? `${actor} restored this test receiver: HTTP 503 → HTTP 200` : "Waiting for the test receiver to become healthy"}</span>
              <span>{replayOccurred
                ? demoMode === "guided" ? "Guided replay created" : "Approved replay created"
                : demoMode === "guided" ? "Guided run will create one replay" : "Operator approval still required"}</span>
              <small>{demoMode === "guided"
                ? "EventHarbor already performed the automatic retries. The guided controller waits for the stopped state, confirms receiver health, and calls the same replay API an operator would use; the original evidence remains unchanged."
                : "EventHarbor already performed the automatic retries. It will not guess whether a resend is safe; your approval creates a traceable recovery delivery while the original evidence remains unchanged."}</small>
            </div>
          ) : null}
          {replayDelivery || replayOccurred ? (
            <AttemptHistory generation={1} delivery={replayDelivery} attempts={replayAttempts} />
          ) : null}
        </section>

        <section className="journey-exchange" aria-labelledby="journey-exchange-title">
          <header>
            <p className="journey-eyebrow">{latestExchange ? "Latest real HTTP exchange" : event ? "Durable acceptance receipt" : "What this will prove"}</p>
            <h3 id="journey-exchange-title">
              {latestExchange ? deliveryRequestName(latestExchange.generation, latestExchange.attempt.attempt_number) : event ? "Stored before network I/O" : scenario.label}
            </h3>
          </header>

          {latestExchange ? (
            <>
              <div className="journey-request-line">
                <span>POST</span><code>{endpointUrl}</code>
              </div>
              <dl className="journey-exchange-meta">
                <div><dt>Event</dt><dd><code>{event ? shortId(event.id) : "Waiting"}</code></dd></div>
                <div><dt>Sent</dt><dd><Timestamp value={latestExchange.attempt.request_timestamp ?? latestExchange.attempt.started_at ?? latestExchange.attempt.created_at} /></dd></div>
              </dl>
              <div className="journey-response" data-tone={responseTone(exchangeResponse)}>
                <span>{latestExchange.attempt.status === "in_progress" ? "Request state" : "Receiver response"}</span>
                <strong>{exchangeResponse ? `HTTP ${exchangeResponse}` : attemptOutcome(latestExchange.attempt)}</strong>
                <p>{exchangeResponse ? responseName(exchangeResponse) : "Waiting for a network result"}</p>
                {latestResponseDetail ? <em>{latestResponseDetail}</em> : null}
                <small>{latestExchange.attempt.duration_ms === null ? "Duration pending" : `${latestExchange.attempt.duration_ms} ms`}</small>
              </div>
              <div className="journey-corroboration" data-matched={Boolean(latestExchange.observation)}>
                <span>{latestExchange.observation ? `Receiver receipt #${latestExchange.observation.sequence}` : "Awaiting independent receiver receipt"}</span>
                <strong>{latestExchange.observation?.body_sha256 === event?.payload_sha256 ? "Exact payload hash observed" : "Correlation pending"}</strong>
              </div>
            </>
          ) : event ? (
            <div className="journey-storage-receipt">
              <strong>HTTP 202 ACCEPTED</strong>
              <span>Event {shortId(event.id)} committed to PostgreSQL</span>
              <small><Timestamp value={event.created_at} /></small>
            </div>
          ) : (
            <div className="journey-preflight">
              <p>{scenario.summary}</p>
              <div>{scenario.sequence.map((step, index) => <span key={`${step}-${index}`}>{step.toUpperCase()}</span>)}</div>
              <small>The sequence is a server-owned test contract. The live history only fills from observed requests.</small>
            </div>
          )}
        </section>
      </div>

      {children ? <div className="journey-actions" data-complete={completed}>{children}<p>{tourMessage}</p></div> : null}

      {completed && event ? (
        <section className="journey-verdict" data-tone={scenario.strategy === "terminal" ? "terminal" : "success"} aria-label="Scenario result">
          <div>
            <p className="journey-eyebrow">Recruiter takeaway</p>
            <h3>{scenario.strategy === "terminal" ? "Correctly stopped. No useless retries." : "Delivered and independently observed."}</h3>
            <p>{scenario.takeaway}</p>
          </div>
          <dl>
            <div><dt>EventHarbor SHA-256</dt><dd><code>{event.payload_sha256}</code></dd></div>
            <div><dt>Receiver Lab SHA-256</dt><dd><code>{proofObservation?.body_sha256 ?? "No receiver hash"}</code></dd></div>
            <div><dt>Comparison</dt><dd>{hashMatches ? "✓ Exact byte match" : "Not available"}</dd></div>
            <div><dt>Final network evidence</dt><dd>{proofObservation ? `HTTP ${proofObservation.response_status_code} · signature header observed` : "Waiting"}</dd></div>
          </dl>
        </section>
      ) : null}

      <div className="journey-details" aria-label="Technical evidence">
        <details>
          <summary><span>View exact canonical JSON and identity</span><b>+</b></summary>
          {event && wireBody ? (
            <div className="journey-detail-content">
              <dl>
                <div><dt>Event ID</dt><dd><code>{event.id}</code></dd></div>
                <div><dt>Stored SHA-256</dt><dd><code>{event.payload_sha256}</code></dd></div>
                <div><dt>Run ID</dt><dd><code>{runId ?? "Not scoped"}</code></dd></div>
              </dl>
              <pre><code>{wireBody}</code></pre>
            </div>
          ) : <p className="journey-detail-empty">The exact bytes appear after the API accepts the event.</p>}
        </details>

        <details>
          <summary><span>View every HTTP attempt</span><b>+</b></summary>
          {exchanges.length ? (
            <div className="journey-attempt-table" role="table" aria-label="All actual HTTP attempts">
              <div role="row"><strong role="columnheader">Delivery</strong><strong role="columnheader">Request</strong><strong role="columnheader">Result</strong><strong role="columnheader">What EventHarbor did</strong><strong role="columnheader">Receiver evidence</strong></div>
              {exchanges.map((exchange) => {
                const decision = attemptDecision(exchange);
                return (
                  <div role="row" key={exchange.attempt.id}>
                    <span role="cell">{exchange.generation === 0 ? "Original" : "Replay"}</span>
                    <span role="cell">#{exchange.attempt.attempt_number}</span>
                    <span className="journey-attempt-result" role="cell"><strong>{attemptOutcome(exchange.attempt)}</strong><small>{exchange.attempt.duration_ms === null ? "Duration pending" : `${exchange.attempt.duration_ms} ms`}</small></span>
                    <span className="journey-attempt-decision" role="cell"><strong>{decision.label}</strong><small>{decision.detail}</small></span>
                    <span role="cell">{exchange.observation ? `Receipt #${exchange.observation.sequence}` : "Awaiting"}</span>
                  </div>
                );
              })}
            </div>
          ) : <p className="journey-detail-empty">No outbound request has been reserved yet.</p>}
        </details>

        <details>
          <summary><span>Why this is real infrastructure</span><b>+</b></summary>
          <div className="journey-real-boundaries">
            <article><span>01</span><strong>FastAPI command</strong><p>The browser receives HTTP 202 only after the event and initial delivery are committed.</p></article>
            <article><span>02</span><strong>PostgreSQL state</strong><p>The background worker claims durable work and records every attempt before and after network I/O.</p></article>
            <article><span>03</span><strong>Separate receiver</strong><p>Receiver Lab independently records what arrived and which HTTP response it returned.</p></article>
          </div>
        </details>
      </div>
    </section>
  );
}
