import { statusLabel } from "../format";
import type { DeliveryStatus } from "../types";

export function StatusPill({ status }: { status: DeliveryStatus }) {
  return <span className={`status-pill status-${status}`}>{statusLabel(status)}</span>;
}

