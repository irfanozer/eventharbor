import { FormEvent, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { getEvents } from "../api";
import { PageHeader } from "../components/PageHeader";
import { Pager } from "../components/Pager";
import { EmptyState, ErrorState, LoadingState } from "../components/QueryState";
import { StatusPill } from "../components/StatusPill";
import { formatDate, shortId } from "../format";
import type { DeliveryStatus } from "../types";

export function EventsPage() {
  const [typeDraft, setTypeDraft] = useState("");
  const [statusDraft, setStatusDraft] = useState<DeliveryStatus | "">("");
  const [filters, setFilters] = useState<{ eventType: string; deliveryStatus: DeliveryStatus | "" }>({ eventType: "", deliveryStatus: "" });
  const [cursors, setCursors] = useState<string[]>([""]);
  const cursor = cursors.at(-1) || undefined;

  const events = useQuery({
    queryKey: ["events", filters, cursor],
    queryFn: () => getEvents({ ...filters, cursor, limit: 25 }),
  });

  function applyFilters(event: FormEvent) {
    event.preventDefault();
    setFilters({ eventType: typeDraft.trim(), deliveryStatus: statusDraft });
    setCursors([""]);
  }

  return (
    <>
      <PageHeader
        index="01 / EVENTS"
        eyebrow="Immutable intake"
        title="Events"
        description="Every accepted payload and its newest delivery generation. Open a row to inspect the complete lineage."
      />

      <form className="filter-bar" onSubmit={applyFilters}>
        <label>
          <span>Event type</span>
          <input value={typeDraft} onChange={(event) => setTypeDraft(event.target.value)} placeholder="demo.order.ready" />
        </label>
        <label>
          <span>Latest status</span>
          <select value={statusDraft} onChange={(event) => setStatusDraft(event.target.value as DeliveryStatus | "")}>
            <option value="">All statuses</option>
            <option value="pending">Pending</option>
            <option value="in_progress">In progress</option>
            <option value="retry_wait">Retry scheduled</option>
            <option value="delivered">Delivered</option>
            <option value="dead_lettered">Dead lettered</option>
          </select>
        </label>
        <button className="secondary-button" type="submit">Apply filters</button>
      </form>

      {events.isPending ? <LoadingState label="Loading recent events" /> : null}
      {events.isError ? <ErrorState error={events.error} retry={() => void events.refetch()} /> : null}
      {events.data?.items.length === 0 ? <EmptyState title="No events match" body="Publish a demo event or loosen the current filters." /> : null}
      {events.data?.items.length ? (
        <div className="data-list" role="table" aria-label="Events">
          <div className="data-list-head" role="row">
            <span role="columnheader">Event</span><span role="columnheader">Endpoint</span><span role="columnheader">Generation</span><span role="columnheader">Accepted</span><span role="columnheader">Status</span>
          </div>
          {events.data.items.map((event) => (
            <Link className="data-row" role="row" key={event.id} to={`/events/${event.id}`}>
              <span role="cell"><strong>{event.type}</strong><small>{shortId(event.id)} · {event.source}</small></span>
              <span role="cell"><strong>{event.endpoint.name}</strong><small>{shortId(event.endpoint.id)}</small></span>
              <span role="cell"><strong>{event.latest_delivery.replay_generation}</strong><small>{event.generation_count} total</small></span>
              <time role="cell" dateTime={event.created_at}>{formatDate(event.created_at)}</time>
              <span role="cell"><StatusPill status={event.latest_delivery.status} /><i aria-hidden="true">→</i></span>
            </Link>
          ))}
        </div>
      ) : null}
      <Pager
        page={cursors.length}
        canGoBack={cursors.length > 1}
        canGoForward={Boolean(events.data?.next_cursor)}
        onBack={() => setCursors((current) => current.slice(0, -1))}
        onForward={() => events.data?.next_cursor && setCursors((current) => [...current, events.data!.next_cursor!])}
      />
    </>
  );
}

