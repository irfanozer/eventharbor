import { ApiError } from "../api";

export function LoadingState({ label = "Loading operational data" }: { label?: string }) {
  return (
    <div className="query-state" role="status">
      <span className="loading-mark" aria-hidden="true" />
      <p>{label}…</p>
    </div>
  );
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const message = error instanceof ApiError || error instanceof Error
    ? error.message
    : "The request could not be completed.";

  return (
    <div className="query-state query-error" role="alert">
      <span className="query-state-label">Request failed</span>
      <p>{message}</p>
      {retry ? <button className="text-button" type="button" onClick={retry}>Try again →</button> : null}
    </div>
  );
}

export function EmptyState({ title, body }: { title: string; body: string }) {
  return (
    <div className="empty-state">
      <span>00 / EMPTY</span>
      <h2>{title}</h2>
      <p>{body}</p>
    </div>
  );
}

