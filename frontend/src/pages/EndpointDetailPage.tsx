import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { getEndpoint, getEvents } from "../api";
import { EmptyState, ErrorState, LoadingState } from "../components/QueryState";
import { StatusPill } from "../components/StatusPill";
import { formatDate, shortId } from "../format";

export function EndpointDetailPage() {
  const { endpointId = "" } = useParams();
  const endpoint = useQuery({
    queryKey: ["endpoint", endpointId],
    queryFn: () => getEndpoint(endpointId),
    enabled: Boolean(endpointId),
  });
  const events = useQuery({
    queryKey: ["events", { endpointId }],
    queryFn: () => getEvents({ endpointId, limit: 25 }),
    enabled: Boolean(endpointId),
  });

  return (
    <>
      <div className="detail-back"><Link to="/endpoints">← All endpoints</Link></div>
      {endpoint.isPending ? <LoadingState label="Loading endpoint metadata" /> : null}
      {endpoint.isError ? <ErrorState error={endpoint.error} retry={() => void endpoint.refetch()} /> : null}
      {endpoint.data ? (
        <>
          <header className="detail-header endpoint-detail-header">
            <div>
              <p className="eyebrow">Endpoint / {shortId(endpoint.data.id)}</p>
              <h1>{endpoint.data.name}</h1>
              <code>{endpoint.data.url}</code>
            </div>
            <dl>
              <div><dt>State</dt><dd>{endpoint.data.enabled ? "Enabled" : "Disabled"}</dd></div>
              <div><dt>Secret version</dt><dd>{endpoint.data.secret_version}</dd></div>
              <div><dt>Registered</dt><dd>{formatDate(endpoint.data.created_at)}</dd></div>
              <div><dt>Updated</dt><dd>{formatDate(endpoint.data.updated_at)}</dd></div>
            </dl>
          </header>
          <section className="detail-section" aria-labelledby="endpoint-events-heading">
            <div className="section-heading-row"><div><p className="eyebrow">Recent traffic</p><h2 id="endpoint-events-heading">Events for this receiver</h2></div></div>
            {events.isPending ? <LoadingState label="Loading endpoint events" /> : null}
            {events.isError ? <ErrorState error={events.error} retry={() => void events.refetch()} /> : null}
            {events.data?.items.length === 0 ? <EmptyState title="No events delivered here" body="This endpoint has not received an accepted EventHarbor event." /> : null}
            {events.data?.items.length ? (
              <div className="data-list compact-data-list">
                {events.data.items.map((item) => (
                  <Link className="data-row" key={item.id} to={`/events/${item.id}`}>
                    <span><strong>{item.type}</strong><small>{shortId(item.id)}</small></span>
                    <time dateTime={item.created_at}>{formatDate(item.created_at)}</time>
                    <span><StatusPill status={item.latest_delivery.status} /><i aria-hidden="true">→</i></span>
                  </Link>
                ))}
              </div>
            ) : null}
          </section>
        </>
      ) : null}
    </>
  );
}

