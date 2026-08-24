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
  takeaway: string;
}

export const DEMO_SCENARIOS: readonly DemoScenario[] = [
  {
    id: "outage_replay",
    label: "Receiver stays offline",
    shortLabel: "Outage + replay",
    preset: "dead_letter",
    strategy: "replay",
    sequence: ["503", "503", "503", "503", "repair", "200"],
    summary: "Receiver Lab stays unavailable until EventHarbor stops safely. The guided demo then repairs only this test run and sends a separate replay.",
    contract: "Receiver Lab rejects four real webhook requests with HTTP 503. EventHarbor stops automatically and preserves the failures. Then this guided demo repairs only the test receiver and creates a separate replay.",
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
    takeaway: "EventHarbor waited instead of hammering a busy API.",
  },
  {
    id: "permanent_rejection",
    label: "Receiver rejects invalid data",
    shortLabel: "Permanent rejection",
    preset: "permanent_failure",
    strategy: "terminal",
    sequence: ["400", "stop"],
    summary: "The destination rejects the request as invalid, so retrying it would be wasteful.",
    contract: "Receiver Lab returns HTTP 400 once. EventHarbor classifies it as terminal and does not make pointless retries.",
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
