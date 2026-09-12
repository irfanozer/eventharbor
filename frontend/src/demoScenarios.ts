import type { DemoScenarioId, ReceiverLabPreset } from "./types";

export type DemoScenarioStrategy = "replay" | "automatic" | "terminal";

export interface DemoScenario {
  id: DemoScenarioId;
  label: string;
  shortLabel: string;
  preset: ReceiverLabPreset;
  strategy: DemoScenarioStrategy;
  sequence: string[];
  summary: string;
  contract: string;
  cause: string;
  takeaway: string;
}

export const DEMO_SCENARIOS: readonly DemoScenario[] = [
  {
    id: "outage_replay",
    label: "Receiver remains unavailable",
    shortLabel: "Outage + replay",
    preset: "dead_letter",
    strategy: "replay",
    sequence: ["503 × 4", "saved + stopped", "health restored", "approved replay", "200"],
    summary: "The destination remains unavailable beyond the automatic retry limit. EventHarbor stops sending but keeps the event and every failed request safe.",
    contract: "Receiver Lab returns HTTP 503 four times. EventHarbor automatically retries, then stops and saves the delivery. After receiver health is restored, one traceable replay succeeds with HTTP 200.",
    cause: "Receiver Lab remains reachable but deliberately returns HTTP 503 to represent a long service outage. Restoring this isolated test receiver changes its response mode to HTTP 200; EventHarbor keeps all earlier evidence unchanged.",
    takeaway: "Automatic retries handle the outage first. Deliberate approval prevents an unsafe surprise resend after the retry window has ended.",
  },
  {
    id: "transient_recovery",
    label: "Receiver recovers briefly",
    shortLabel: "Transient outage",
    preset: "retry_then_recover",
    strategy: "automatic",
    sequence: ["503", "503", "200"],
    summary: "The destination is temporarily unavailable and recovers during the automatic retry window.",
    contract: "Receiver Lab returns HTTP 503 twice, then HTTP 200. The original delivery recovers automatically without manual intervention or replay.",
    cause: "Requests 1 and 2 return HTTP 503. Request 3 returns HTTP 200, simulating a short service interruption that clears by itself.",
    takeaway: "EventHarbor survived a temporary outage without human action.",
  },
  {
    id: "rate_limit_recovery",
    label: "Receiver asks us to slow down",
    shortLabel: "Rate limited",
    preset: "rate_limit_then_recover",
    strategy: "automatic",
    sequence: ["429", "429", "200"],
    summary: "The destination asks EventHarbor to slow down before accepting the request.",
    contract: "Receiver Lab returns HTTP 429 twice with Retry-After: 5, then HTTP 200. The worker follows the receiver's timing.",
    cause: "Requests 1 and 2 return HTTP 429 with Retry-After: 5 seconds. The worker waits instead of sending continuously; request 3 is accepted.",
    takeaway: "EventHarbor waited instead of hammering a busy API.",
  },
  {
    id: "permanent_rejection",
    label: "Receiver rejects invalid data",
    shortLabel: "Permanent rejection",
    preset: "success",
    strategy: "terminal",
    sequence: ["400 · required field missing", "saved + stopped"],
    summary: "The selected event deliberately omits one field required by its receiver route. The HTTP request is real; the schema rejection comes from the body itself.",
    contract: "EventHarbor accepts and stores the valid event envelope, then Receiver Lab validates the selected business-event contract and returns an exact HTTP 400 missing-field response.",
    cause: "The composer names the omitted field before you send. Receiver Lab rejects that exact JSON body even though it is otherwise online and healthy.",
    takeaway: "EventHarbor distinguishes a temporary infrastructure failure from a permanent client-data error and avoids pointless retries.",
  },
] as const;

export const DEFAULT_DEMO_SCENARIO_ID: DemoScenarioId = "outage_replay";

export function isDemoScenarioId(value: unknown): value is DemoScenarioId {
  return typeof value === "string" && DEMO_SCENARIOS.some((scenario) => scenario.id === value);
}

export function demoScenario(id: DemoScenarioId): DemoScenario {
  return DEMO_SCENARIOS.find((scenario) => scenario.id === id) ?? DEMO_SCENARIOS[0]!;
}
