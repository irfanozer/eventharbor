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
    label: "Receiver stays offline",
    shortLabel: "Outage + replay",
    preset: "dead_letter",
    strategy: "replay",
    sequence: ["503", "503", "503", "503", "stop", "503 → 200", "replay"],
    summary: "A controlled fault keeps Receiver Lab at HTTP 503 until EventHarbor stops safely. The test receiver is then switched to HTTP 200 and a separate replay is created.",
    contract: "Receiver Lab rejects four real webhook requests with HTTP 503. EventHarbor stops automatically and preserves the failures. Then the demo control changes only this test run to HTTP 200 before creating a separate replay.",
    cause: "Controlled fault: Receiver Lab is pinned to HTTP 503. Switching it to HTTP 200 simulates the destination coming back after a restart or fixed deployment; it does not alter EventHarbor or resend the stopped delivery.",
    takeaway: "EventHarbor contained the outage, preserved every failure, then replayed safely after repair.",
  },
  {
    id: "transient_recovery",
    label: "Receiver recovers briefly",
    shortLabel: "Transient outage",
    preset: "retry_then_recover",
    strategy: "automatic",
    sequence: ["503", "503", "200"],
    summary: "The destination is temporarily unavailable and recovers during the automatic retry window.",
    contract: "Receiver Lab returns HTTP 503 twice, then HTTP 200. The original delivery recovers automatically without a repair or replay.",
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
    contract: "Receiver Lab returns HTTP 429 twice with Retry-After: 2, then HTTP 200. The worker follows the receiver's timing.",
    cause: "Requests 1 and 2 return HTTP 429 with Retry-After: 2 seconds. The worker waits instead of sending continuously; request 3 is accepted.",
    takeaway: "EventHarbor waited instead of hammering a busy API.",
  },
  {
    id: "permanent_rejection",
    label: "Receiver rejects invalid data",
    shortLabel: "Permanent rejection",
    preset: "permanent_failure",
    strategy: "terminal",
    sequence: ["400 · missing customer_id", "stop"],
    summary: "This test order intentionally omits a field required by Receiver Lab, so retrying the unchanged body would be wasteful.",
    contract: "Receiver Lab validates the real JSON body, finds that data.customer_id is missing, and returns HTTP 400 missing_customer_id. EventHarbor stops after that one request.",
    cause: "Concrete validation error: this event intentionally has no data.customer_id. Receiver Lab returns the exact missing-field reason in its HTTP response.",
    takeaway: "EventHarbor knew that retrying could not fix invalid data.",
  },
] as const;

export const DEFAULT_DEMO_SCENARIO_ID: DemoScenarioId = "outage_replay";

export function isDemoScenarioId(value: unknown): value is DemoScenarioId {
  return typeof value === "string" && DEMO_SCENARIOS.some((scenario) => scenario.id === value);
}

export function demoScenario(id: DemoScenarioId): DemoScenario {
  return DEMO_SCENARIOS.find((scenario) => scenario.id === id) ?? DEMO_SCENARIOS[0]!;
}
