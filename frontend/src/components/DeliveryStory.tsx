import type { ReactNode } from "react";
import { demoEventDefinition, isExpectedSchemaRejection } from "../demoEvents";
import type { DemoScenario } from "../demoScenarios";
import type { Delivery, DeliveryAttempt, EventDetail, ReceiverLabRequest } from "../types";

export interface DeliveryStoryProps {
  scenario: DemoScenario;
  event: EventDetail | null;
  original: Delivery | undefined;
  replay: Delivery | undefined;
  originalAttempts: DeliveryAttempt[];
  replayAttempts: DeliveryAttempt[];
  receipts: ReceiverLabRequest[];
  receiverReady: boolean;
  starting: boolean;
  reading: boolean;
  evidenceUnavailable: boolean;
  children?: ReactNode;
}

function recorded(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "Time unavailable" : date.toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
}

export function responseLabel(attempt: DeliveryAttempt): string {
  if (attempt.status === "in_progress") return "Waiting for a response";
  if (attempt.status === "indeterminate") return "Outcome unknown";
  const code = attempt.http_status_code;
  if (code !== null && code >= 200 && code < 300) return `${code} · Accepted`;
  if (code === 503) return "503 · Service unavailable";
  if (code === 429) return "429 · Slow down";
  if (code === 400) return "400 · Request rejected";
  return code !== null ? `${code} · HTTP response` : attempt.error_type?.replaceAll("_", " ") ?? "No response recorded";
}

interface AttemptRow { attempt: DeliveryAttempt; delivery: Delivery | undefined; replay: boolean }

function attemptExplanation({ attempt, delivery }: AttemptRow): string {
  if (attempt.status === "in_progress") return "The delivery service is handling this attempt. No final response has been recorded yet.";
  if (attempt.status === "indeterminate") return "This attempt's outcome was not confirmed. The receiver may have received it.";
  const code = attempt.http_status_code;
  if (code !== null && code >= 200 && code < 300) return "The receiver accepted the event. This delivery is complete.";
  if (attempt.disposition === "terminal_failure") return "Automatic retries stopped. This request needs attention before sending again.";
  if (delivery?.status === "dead_lettered" && attempt.attempt_number === delivery.attempt_count) {
    return "Automatic retries ended here. The event is still saved.";
  }
  if (attempt.retry_scheduled_for) return `A retry was scheduled for ${recorded(attempt.retry_scheduled_for)}. The same event was kept.`;
  if (attempt.disposition === "retry") return "This failure was recorded as retryable. The event was kept for another attempt.";
  return "This outcome was saved in the delivery history.";
}

function DeliveryHistory({ event, rows, evidenceUnavailable, recordsPending, finished }: {
  event: EventDetail; rows: AttemptRow[]; evidenceUnavailable: boolean; recordsPending: boolean; finished: boolean;
}) {
  return <section className="attempt-feed" aria-labelledby="delivery-history-title">
    <header><h3 id="delivery-history-title">What happened to this event</h3><span>{rows.length} {rows.length === 1 ? "attempt" : "attempts"}</span></header>
    <p className="history-intro">Actual requests, in order. Results stay here after delivery finishes.</p>
    <div className="history-saved">
      <strong>Saved by EventHarbor</strong>
      <time dateTime={event.created_at} title={new Date(event.created_at).toLocaleString()}>{recorded(event.created_at)}</time>
      <p>Your browser sent the event. EventHarbor stored it before trying to deliver it.</p>
    </div>
    {evidenceUnavailable && <p className="evidence-warning" role="status">Attempt records are temporarily unavailable. Previously loaded records may be out of date.</p>}
    {rows.length > 0 && <ol className="request-history" aria-label="Recorded delivery attempts">{rows.map((row) => {
      const { attempt, replay: isReplay } = row;
      const accepted = attempt.status === "completed" && attempt.http_status_code !== null && attempt.http_status_code >= 200 && attempt.http_status_code < 300;
      const timestamp = attempt.started_at ?? attempt.created_at;
      return <li key={attempt.id} data-tone={attempt.status === "in_progress" ? "pending" : accepted ? "success" : "attention"}>
        {isReplay && attempt.attempt_number === 1 && <p className="history-resend">A new delivery of the same saved event</p>}
        <div className="request-history__heading">
          <h4><span>{isReplay ? "Resend" : "Try"} {attempt.attempt_number}</span>{" "}<strong>{responseLabel(attempt)}</strong></h4>
          <time dateTime={timestamp} title={new Date(timestamp).toLocaleString()}>{recorded(timestamp)}</time>
        </div>
        <p>{attemptExplanation(row)}</p>
      </li>;
    })}</ol>}
    {!evidenceUnavailable && recordsPending && <p className="history-pending" role="status">Loading the remaining attempt records…</p>}
    {!evidenceUnavailable && !recordsPending && rows.length === 0 && <p className="history-pending" role="status">{finished ? "No HTTP attempts were recorded for this delivery." : "Waiting for the first recorded attempt…"}</p>}
  </section>;
}

export function DeliveryStory(props: DeliveryStoryProps) {
  const { event, original, replay, scenario, receiverReady, originalAttempts, replayAttempts } = props;
  const current = replay ?? original;
  const rows = [
    ...originalAttempts.map((attempt) => ({ attempt, delivery: original, replay: false })),
    ...replayAttempts.map((attempt) => ({ attempt, delivery: replay, replay: true })),
  ].sort((a, b) => Number(a.replay) - Number(b.replay) || a.attempt.attempt_number - b.attempt.attempt_number);
  const recordsPending = [original, replay].some((delivery) => delivery &&
    (rows.filter((row) => row.delivery?.id === delivery.id).length < delivery.attempt_count ||
      (["delivered", "dead_lettered"].includes(delivery.status) &&
        rows.some((row) => row.delivery?.id === delivery.id && row.attempt.status === "in_progress"))));
  const schemaRejected = isExpectedSchemaRejection(event?.type, original, originalAttempts);
  const delivered = current?.status === "delivered";
  const stopped = current?.status === "dead_lettered";
  const title = delivered ? replay ? "The saved event was delivered." : "The event was delivered."
    : stopped && recordsPending ? "Delivery stopped. Loading the recorded response."
    : schemaRejected ? "Invalid data. No pointless retries."
    : stopped && scenario.strategy === "replay" && !replay
      ? receiverReady ? "Receiver ready. You decide when to resend." : "Delivery stopped. Your event is safe."
    : stopped ? "Delivery stopped. Review the response."
    : current?.status === "retry_wait" ? "Not accepted yet. Another try is scheduled."
    : event ? "Event saved. Following the delivery."
    : props.starting ? "Sending your sample event…"
    : props.reading ? "Reading your saved run…" : "Ready when you are.";
  const explanation = delivered ? replay
    ? "The resend succeeded. The original attempts are still shown below."
    : "EventHarbor completed this delivery without a manual resend. Its attempts are shown below."
    : stopped && recordsPending ? "The delivery status is saved. Its final request records are still being loaded."
    : schemaRejected ? `The receiver requires ${demoEventDefinition(event?.type).invalidField}. Sending the same incomplete data again would not help.`
    : stopped && scenario.strategy === "replay" && !replay
      ? receiverReady ? "Restoring the test receiver did not resend anything. Approve one new delivery when you are ready."
        : "Automatic retries have ended. First make this test receiver available again, then choose whether to resend."
    : stopped ? "The stored outcome did not complete this case as expected. Inspect the actual response before trying a new event."
    : current?.status === "retry_wait" ? "The background delivery service waits before trying again. You do not need to press Send twice."
    : event ? "The event is in the database. Each attempt will appear below as it is recorded."
    : props.starting ? "Configuring this isolated test receiver and asking EventHarbor to save the event."
    : props.reading ? "This only reads the existing event. It does not send another one."
    : "Choose a case and send one sample event. Its actual responses will appear here and stay visible for you to read.";
  const matchedReceipts = rows.filter(({ delivery, attempt }) => delivery && attempt.status === "completed" &&
    props.receipts.some((receipt) => receipt.event_id === event?.id && receipt.delivery_id === delivery.id &&
      receipt.delivery_attempt === attempt.attempt_number && receipt.response_status_code === attempt.http_status_code)).length;

  return (
    <section className="delivery-story" id="live-proof" aria-labelledby="delivery-story-title" tabIndex={-1}>
      <header className="delivery-story__header"><span className="eyebrow">2 / Follow the result</span><span className="demo-tag">{event ? "Stored event" : "No event sent yet"}</span></header>
      <div className="delivery-route" aria-label="How this request moves">
        <div data-done={Boolean(event)}><strong>Your browser</strong><small>Sends the event</small></div>
        <span aria-hidden="true">→</span>
        <div data-done={Boolean(event)}><strong>EventHarbor</strong><small>Saves it, then retries</small></div>
        <span aria-hidden="true">→</span>
        <div data-done={delivered}><strong>Test receiver</strong><small>Accepts or rejects it</small></div>
      </div>
      <div className="delivery-outcome" data-tone={delivered ? "success" : stopped ? "stopped" : "waiting"}>
        {event && <span className="live-status-label">Latest delivery status</span>}
        <div role="status" aria-live="polite"><h2 id="delivery-story-title">{title}</h2><p>{explanation}</p></div>
        {props.children}
      </div>
      {event && <DeliveryHistory event={event} rows={rows} evidenceUnavailable={props.evidenceUnavailable} recordsPending={recordsPending} finished={delivered || stopped} />}
      {event && <p className="attempt-proof">Event <code>{event.id.slice(0, 8)}</code> · {matchedReceipts ? `${matchedReceipts} ${matchedReceipts === 1 ? "response also found" : "responses also found"} in the receiver’s log.` : "Receiver-side receipts will appear when available."}</p>}
    </section>
  );
}
