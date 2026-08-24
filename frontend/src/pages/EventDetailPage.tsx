import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { getEvent } from "../api";
import { EventTimeline } from "../components/EventTimeline";
import { ErrorState, LoadingState } from "../components/QueryState";
import { formatDate, prettyJson, shortId } from "../format";

export function EventDetailPage() {
  const { eventId = "" } = useParams();
  const event = useQuery({
    queryKey: ["event", eventId],
    queryFn: () => getEvent(eventId),
    enabled: Boolean(eventId),
    refetchInterval: (query) => query.state.data?.deliveries.some((delivery) => !["delivered", "dead_lettered"].includes(delivery.status)) ? 1_000 : false,
  });

  return (
    <>
      <div className="detail-back"><Link to="/events">← All events</Link></div>
      {event.isPending ? <LoadingState label="Loading event lineage" /> : null}
      {event.isError ? <ErrorState error={event.error} retry={() => void event.refetch()} /> : null}
      {event.data ? (
        <>
          <header className="detail-header">
            <div>
              <p className="eyebrow">Event / {shortId(event.data.id)}</p>
              <h1>{event.data.type}</h1>
              <p>Accepted from {event.data.source} on {formatDate(event.data.created_at)}.</p>
            </div>
            <dl>
              <div><dt>Stable event ID</dt><dd>{event.data.id}</dd></div>
              <div><dt>Generations</dt><dd>{event.data.deliveries.length}</dd></div>
            </dl>
          </header>

          <section className="detail-section" aria-labelledby="delivery-lineage-heading">
            <div className="section-heading-row"><div><p className="eyebrow">Persistent evidence</p><h2 id="delivery-lineage-heading">Delivery lineage</h2></div></div>
            <EventTimeline deliveries={event.data.deliveries} />
          </section>

          <section className="evidence-grid" aria-label="Event evidence">
            <article>
              <p className="eyebrow">Accepted payload</p>
              <pre><code>{prettyJson(event.data.data)}</code></pre>
            </article>
            <article>
              <p className="eyebrow">Integrity evidence</p>
              <dl className="evidence-list">
                <div><dt>Payload SHA-256</dt><dd>{event.data.payload_sha256}</dd></div>
                <div><dt>Request fingerprint</dt><dd>{event.data.request_fingerprint_sha256}</dd></div>
                <div><dt>Idempotency key</dt><dd>{event.data.idempotency_key}</dd></div>
              </dl>
            </article>
          </section>
        </>
      ) : null}
    </>
  );
}

