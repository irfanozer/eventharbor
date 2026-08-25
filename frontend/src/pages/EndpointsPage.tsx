import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { getEndpoints } from "../api";
import { RECEIVER_LAB_BASE_URL, isCanonicalDemoEndpoint } from "../demoEvents";
import { PageHeader } from "../components/PageHeader";
import { Pager } from "../components/Pager";
import { EmptyState, ErrorState, LoadingState } from "../components/QueryState";
import { formatDate, shortId } from "../format";
import type { Endpoint } from "../types";

function EndpointCards({ endpoints, startAt = 0 }: { endpoints: Endpoint[]; startAt?: number }) {
  return (
    <div className="endpoint-grid">
      {endpoints.map((endpoint, index) => (
        <Link to={`/endpoints/${endpoint.id}`} className="endpoint-card" key={endpoint.id}>
          <header><span>{String(startAt + index + 1).padStart(2, "0")}</span><span className={endpoint.enabled ? "endpoint-on" : "endpoint-off"}>{endpoint.enabled ? "Enabled" : "Disabled"}</span></header>
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
  );
}

export function EndpointsPage() {
  const [cursors, setCursors] = useState<string[]>([""]);
  const cursor = cursors.at(-1) || undefined;
  const endpoints = useQuery({
    queryKey: ["endpoints", cursor],
    queryFn: () => getEndpoints({ cursor, limit: 25 }),
  });
  const canonicalEndpoints = endpoints.data?.items.filter((endpoint) => isCanonicalDemoEndpoint(endpoint.url)) ?? [];
  const visibleCanonicalEndpoints = canonicalEndpoints.filter(
    (endpoint, index, all) => all.findIndex((candidate) => candidate.url === endpoint.url) === index,
  );
  const duplicateCanonicalEndpoints = canonicalEndpoints.filter(
    (endpoint, index, all) => all.findIndex((candidate) => candidate.url === endpoint.url) !== index,
  );
  const legacyEndpoints = endpoints.data?.items.filter((endpoint) => endpoint.url === RECEIVER_LAB_BASE_URL) ?? [];
  const otherEndpoints = endpoints.data?.items.filter(
    (endpoint) => !isCanonicalDemoEndpoint(endpoint.url) && endpoint.url !== RECEIVER_LAB_BASE_URL,
  ) ?? [];
  const visibleEndpoints = [...visibleCanonicalEndpoints, ...otherEndpoints];
  const historicalEndpoints = [...duplicateCanonicalEndpoints, ...legacyEndpoints];

  return (
    <>
      <PageHeader
        index="03 / ENDPOINTS"
        eyebrow="Delivery destinations"
        title="Named receiver routes"
        description="Orders, shipping, and inventory use distinct webhook paths inside the separate Receiver Lab service. The signing secret is revealed once at registration and never returned by these read APIs."
      />
      {endpoints.isPending ? <LoadingState label="Loading registered endpoints" /> : null}
      {endpoints.isError ? <ErrorState error={endpoints.error} retry={() => void endpoints.refetch()} /> : null}
      {endpoints.data?.items.length === 0 ? <EmptyState title="No endpoints registered" body="Run the reliability story to register the deterministic Receiver Lab." /> : null}
      {visibleEndpoints.length ? <EndpointCards endpoints={visibleEndpoints} /> : null}
      {historicalEndpoints.length ? (
        <details className="legacy-endpoints">
          <summary>Earlier or duplicate registrations <span>{historicalEndpoints.length}</span></summary>
          <p>These records come from earlier local iterations and smoke checks. Rows sharing one named receiver URL are duplicate registrations; <code>/webhooks</code> is the old generic route. They remain as historical data, not separate receiver systems.</p>
          <EndpointCards endpoints={historicalEndpoints} startAt={visibleEndpoints.length} />
        </details>
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
