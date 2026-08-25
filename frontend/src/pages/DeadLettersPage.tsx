import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { getDeadLetters } from "../api";
import { PageHeader } from "../components/PageHeader";
import { Pager } from "../components/Pager";
import { EmptyState, ErrorState, LoadingState } from "../components/QueryState";
import { formatDate, shortId } from "../format";

export function DeadLettersPage() {
  const [cursors, setCursors] = useState<string[]>([""]);
  const cursor = cursors.at(-1) || undefined;
  const deadLetters = useQuery({
    queryKey: ["dead-letters", cursor],
    queryFn: () => getDeadLetters({ cursor, limit: 25 }),
    refetchInterval: 3_000,
  });

  return (
    <>
      <PageHeader
        index="02 / STOPPED DELIVERIES"
        eyebrow="Saved events awaiting review"
        title="Stopped, saved, ready for review."
        description="EventHarbor calls this state dead-lettered: automatic sending has ended, but the event and every HTTP attempt remain stored. Only the latest delivery requiring action appears here."
        action={<Link className="primary-link" to="/">Run reliability story →</Link>}
      />
      {deadLetters.isPending ? <LoadingState label="Loading actionable failures" /> : null}
      {deadLetters.isError ? <ErrorState error={deadLetters.error} retry={() => void deadLetters.refetch()} /> : null}
      {deadLetters.data?.items.length === 0 ? (
        <EmptyState title="No operator action required" body="Every event's newest delivery is either active or delivered." />
      ) : null}
      {deadLetters.data?.items.length ? (
        <div className="dead-letter-grid">
          {deadLetters.data.items.map((item, index) => {
            const correctionRequired = item.blocked_code === "payload_correction_required";
            return (
              <article key={item.delivery.id} className="dead-letter-card">
                <header><span>{String(index + 1).padStart(2, "0")}</span><strong>Stopped · saved</strong></header>
                <h2>{item.event_type}</h2>
                <p>{correctionRequired
                  ? <>The receiver permanently rejected the event sent to <strong>{item.endpoint.name}</strong>. Publish a corrected event; EventHarbor blocks an unchanged replay.</>
                  : <>Delivery to <strong>{item.endpoint.name}</strong> reached its automatic retry limit after {item.delivery.attempt_count} real requests.</>}</p>
                <dl>
                  <div><dt>Event</dt><dd>{shortId(item.event_id)}</dd></div>
                  <div><dt>Delivery</dt><dd>{item.delivery.replay_generation === 0 ? "Original" : `Recovery replay ${item.delivery.replay_generation}`}</dd></div>
                  <div><dt>Last changed</dt><dd>{formatDate(item.delivery.updated_at)}</dd></div>
                  <div><dt>Next action</dt><dd>{item.replayable ? "Approved replay available" : item.blocked_reason ?? "Review required"}</dd></div>
                  <div><dt>Database status</dt><dd><code>dead_lettered</code></dd></div>
                </dl>
                {item.delivery.last_error ? <div className="failure-evidence"><span>Last worker evidence</span><code>{item.delivery.last_error}</code></div> : null}
                <div className="dead-letter-actions">
                  {correctionRequired ? <Link to={`/?event_type=${encodeURIComponent(item.event_type)}&new_event=1`}>Open a fresh corrected draft <span aria-hidden="true">→</span></Link> : null}
                  <Link to={`/events/${item.event_id}`}>Inspect full lineage <span aria-hidden="true">→</span></Link>
                </div>
              </article>
            );
          })}
        </div>
      ) : null}
      <Pager
        page={cursors.length}
        canGoBack={cursors.length > 1}
        canGoForward={Boolean(deadLetters.data?.next_cursor)}
        onBack={() => setCursors((current) => current.slice(0, -1))}
        onForward={() => deadLetters.data?.next_cursor && setCursors((current) => [...current, deadLetters.data!.next_cursor!])}
      />
    </>
  );
}
