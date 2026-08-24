import { Link } from "react-router-dom";

export function NotFoundPage() {
  return (
    <section className="not-found">
      <span>404 / UNKNOWN ROUTE</span>
      <h1>Nothing is routed here.</h1>
      <p>The durable records are safe. This page simply does not exist.</p>
      <Link className="primary-link" to="/">Return to Control Room →</Link>
    </section>
  );
}

