import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { getEndpoints } from "../api";
import { PageHeader } from "../components/PageHeader";
import { Pager } from "../components/Pager";
import { EmptyState, ErrorState, LoadingState } from "../components/QueryState";
import { formatDate, shortId } from "../format";

export function EndpointsPage() {
  const [cursors, setCursors] = useState<string[]>([""]);
  const cursor = cursors.at(-1) || undefined;
  const endpoints = useQuery({
    queryKey: ["endpoints", cursor],
    queryFn: () => getEndpoints({ cursor, limit: 25 }),
  });

  return (
    <>
      <PageHeader
        index="03 / ENDPOINTS"
        eyebrow="Delivery destinations"
        title="Registered receivers"
        description="Public endpoint metadata only. Signing material is never returned by these read APIs or stored by Control Room."
      />
      {endpoints.isPending ? <LoadingState label="Loading registered endpoints" /> : null}
      {endpoints.isError ? <ErrorState error={endpoints.error} retry={() => void endpoints.refetch()} /> : null}
      {endpoints.data?.items.length === 0 ? <EmptyState title="No endpoints registered" body="Run the reliability story to register the deterministic Receiver Lab." /> : null}
      {endpoints.data?.items.length ? (
        <div className="endpoint-grid">
          {endpoints.data.items.map((endpoint, index) => (
            <Link to={`/endpoints/${endpoint.id}`} className="endpoint-card" key={endpoint.id}>
              <header><span>{String(index + 1).padStart(2, "0")}</span><span className={endpoint.enabled ? "endpoint-on" : "endpoint-off"}>{endpoint.enabled ? "Enabled" : "Disabled"}</span></header>
              <h2>{endpoint.name}</h2>
              <code>{endpoint.url}</code>
              <dl>
                <div><dt>Endpoint ID</dt><dd>{shortId(endpoint.id)}</dd></div>
                <div><dt>Secret version</dt><dd>{endpoint.secret_version}</dd></div>
                <div><dt>Registered</dt><dd>{formatDate(endpoint.created_at)}</dd></div>
              </dl>
              <span className="card-link">Inspect endpoint <i aria-hidden="true">→</i></span>
            </Link>
          ))}
        </div>
      ) : null}
      <Pager
        page={cursors.length}
        canGoBack={cursors.length > 1}
        canGoForward={Boolean(endpoints.data?.next_cursor)}
        onBack={() => setCursors((current) => current.slice(0, -1))}
        onForward={() => endpoints.data?.next_cursor && setCursors((current) => [...current, endpoints.data!.next_cursor!])}
      />
    </>
  );
}

