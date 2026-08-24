import { useQuery } from "@tanstack/react-query";

import { getDeliveryAttempts } from "../api";
import { dispositionLabel, formatDate, formatRelative } from "../format";
import type { Delivery, DeliveryAttempt } from "../types";
import { ErrorState, LoadingState } from "./QueryState";
import { StatusPill } from "./StatusPill";

function attemptOutcome(attempt: DeliveryAttempt): string {
  if (attempt.http_status_code) return `HTTP ${attempt.http_status_code}`;
  if (attempt.error_type) return attempt.error_type;
  if (attempt.status === "in_progress") return "Request in flight";
  return dispositionLabel(attempt.disposition);
}

function DeliveryGeneration({ delivery }: { delivery: Delivery }) {
  const terminal = delivery.status === "delivered" || delivery.status === "dead_lettered";
  const attempts = useQuery({
    queryKey: ["delivery-attempts", delivery.id],
    queryFn: () => getDeliveryAttempts(delivery.id),
    refetchInterval: terminal ? false : 1_000,
  });

  return (
    <article className={`delivery-generation generation-${delivery.status}`}>
      <header>
        <div>
          <span className="generation-label">{delivery.replay_generation === 0 ? "Original delivery" : "Recovery replay"}</span>
          <p>{delivery.replay_generation === 0 ? "Created when the event was accepted" : "Created separately after the original delivery stopped"}</p>
        </div>
        <StatusPill status={delivery.status} />
      </header>

      <dl className="generation-facts">
        <div><dt>Attempts</dt><dd>{delivery.attempt_count}</dd></div>
        <div><dt>Last change</dt><dd title={formatDate(delivery.updated_at)}>{formatRelative(delivery.updated_at)}</dd></div>
        <div><dt>Next attempt</dt><dd>{formatRelative(delivery.next_attempt_at)}</dd></div>
      </dl>

      {delivery.last_error ? <p className="delivery-error">{delivery.last_error}</p> : null}

      {attempts.isPending ? <LoadingState label="Loading persisted attempts" /> : null}
      {attempts.isError ? <ErrorState error={attempts.error} retry={() => void attempts.refetch()} /> : null}
      {attempts.data ? (
        attempts.data.attempts.length ? (
          <ol className="attempt-timeline" aria-label={`${delivery.replay_generation === 0 ? "Original delivery" : "Recovery replay"} attempts`}>
            {attempts.data.attempts.map((attempt) => (
              <li key={attempt.id}>
                <span className="attempt-number">{String(attempt.attempt_number).padStart(2, "0")}</span>
                <div className="attempt-copy">
                  <strong>{attemptOutcome(attempt)}</strong>
                  <span>{dispositionLabel(attempt.disposition)}</span>
                  {attempt.error_message ? <small>{attempt.error_message}</small> : null}
                </div>
                <div className="attempt-metrics">
                  <span>{attempt.duration_ms === null ? "—" : `${attempt.duration_ms} ms`}</span>
                  <time dateTime={attempt.created_at}>{formatRelative(attempt.created_at)}</time>
                </div>
              </li>
            ))}
          </ol>
        ) : (
          <p className="quiet-state">Accepted durably. The worker has not reserved an attempt yet.</p>
        )
      ) : null}
    </article>
  );
}

export function EventTimeline({ deliveries }: { deliveries: Delivery[] }) {
  const ordered = [...deliveries].sort((left, right) => left.replay_generation - right.replay_generation);
  return (
    <section className="event-timeline" aria-label="Original delivery and recovery replay lineage">
      {ordered.map((delivery, index) => (
        <div className="lineage-item" key={delivery.id}>
          {index > 0 ? (
            <div className="replay-bridge">
              <span>Replay accepted</span>
              <small>Prior evidence preserved</small>
            </div>
          ) : null}
          <DeliveryGeneration delivery={delivery} />
        </div>
      ))}
    </section>
  );
}
