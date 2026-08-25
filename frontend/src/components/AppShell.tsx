import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet } from "react-router-dom";

import { getOverview } from "../api";

const navigation = [
  { to: "/", label: "Control room", end: true },
  { to: "/events", label: "Events", end: false },
  { to: "/dead-letters", label: "Stopped deliveries", end: false },
  { to: "/endpoints", label: "Endpoints", end: false },
];

export function AppShell() {
  const health = useQuery({
    queryKey: ["overview"],
    queryFn: getOverview,
    refetchInterval: 5_000,
  });

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Skip to content</a>
      <header className="masthead">
        <NavLink className="wordmark" to="/" aria-label="EventHarbor Control Room home">
          EH<span>/</span> CONTROL ROOM
        </NavLink>
        <nav aria-label="Primary navigation">
          {navigation.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => (isActive ? "active" : undefined)}
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div
          className={`reachability ${health.isError ? "reachability-down" : ""}`}
          aria-live="polite"
        >
          <span className="signal" aria-hidden="true" />
          {health.isError ? "API unavailable" : health.data ? "API reachable" : "Checking API"}
        </div>
      </header>
      <main id="main-content">
        <Outlet />
      </main>
      <footer className="site-footer">
        <strong>EventHarbor</strong>
        <p>Durable webhook delivery, observable failure, controlled recovery.</p>
        <span>Control Room / local demonstration</span>
      </footer>
    </div>
  );
}
