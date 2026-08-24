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
        index="02 / DEAD LETTERS"
        eyebrow="Operator action queue"
        title="Failure has a boundary."
        description="Only latest-generation terminal failures appear here. Superseded generations remain on the event, but leave the action queue."
        action={<Link className="primary-link" to="/">Run reliability story →</Link>}
      />
      {deadLetters.isPending ? <LoadingState label="Loading actionable failures" /> : null}
      {deadLetters.isError ? <ErrorState error={deadLetters.error} retry={() => void deadLetters.refetch()} /> : null}
      {deadLetters.data?.items.length === 0 ? (
        <EmptyState title="No operator action required" body="The latest generation of every event is either active or delivered." />
      ) : null}
      {deadLetters.data?.items.length ? (
        <div className="dead-letter-grid">
          {deadLetters.data.items.map((item, index) => (
            <article key={item.delivery.id} className="dead-letter-card">
              <header><span>{String(index + 1).padStart(2, "0")}</span><strong>Dead lettered</strong></header>
              <h2>{item.event_type}</h2>
              <p>Delivery to <strong>{item.endpoint.name}</strong> exhausted its retry policy after {item.delivery.attempt_count} attempts.</p>
              <dl>
                <div><dt>Event</dt><dd>{shortId(item.event_id)}</dd></div>
                <div><dt>Generation</dt><dd>{item.delivery.replay_generation}</dd></div>
                <div><dt>Last changed</dt><dd>{formatDate(item.delivery.updated_at)}</dd></div>
                <div><dt>Replay</dt><dd>{item.replayable ? "Eligible" : item.blocked_reason ?? "Blocked"}</dd></div>
              </dl>
              {item.delivery.last_error ? <div className="failure-evidence"><span>Last worker evidence</span><code>{item.delivery.last_error}</code></div> : null}
              <Link to={`/events/${item.event_id}`}>Inspect full lineage <span aria-hidden="true">→</span></Link>
            </article>
          ))}
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

