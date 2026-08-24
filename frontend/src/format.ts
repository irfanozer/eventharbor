import type { DeliveryDisposition, DeliveryStatus } from "./types";

export function shortId(value: string, head = 8): string {
  return value.length <= head ? value : `${value.slice(0, head)}…${value.slice(-4)}`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "Not yet";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(value));
}

export function formatRelative(value: string | null | undefined): string {
  if (!value) return "Not yet";
  const delta = new Date(value).getTime() - Date.now();
  const absolute = Math.abs(delta);
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (absolute < 60_000) return formatter.format(Math.round(delta / 1_000), "second");
  if (absolute < 3_600_000) return formatter.format(Math.round(delta / 60_000), "minute");
  if (absolute < 86_400_000) return formatter.format(Math.round(delta / 3_600_000), "hour");
  return formatter.format(Math.round(delta / 86_400_000), "day");
}

export function statusLabel(status: DeliveryStatus): string {
  const labels: Record<DeliveryStatus, string> = {
    pending: "Pending",
    in_progress: "In progress",
    retry_wait: "Retry scheduled",
    delivered: "Delivered",
    dead_lettered: "Dead lettered",
  };
  return labels[status];
}

export function dispositionLabel(disposition: DeliveryDisposition | null): string {
  if (!disposition) return "Awaiting outcome";
  const labels: Record<DeliveryDisposition, string> = {
    succeeded: "Succeeded",
    retry: "Retry requested",
    terminal_failure: "Terminal failure",
  };
  return labels[disposition];
}

export function prettyJson(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

