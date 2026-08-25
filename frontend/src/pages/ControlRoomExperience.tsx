import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";

import {
  ensureReceiverLabEndpoint,
  getDeliveryAttempts,
  getEndpoint,
  getEvent,
  getReceiverLab,
  publishDemoEvent,
  replayDelivery,
  setReceiverLabPreset,
} from "../api";
import { EventJourney } from "../components/EventJourney";
import { EventTimeline } from "../components/EventTimeline";
import { ErrorState } from "../components/QueryState";
import { ReplayConfirmation } from "../components/ReplayConfirmation";
import {
  DEFAULT_DEMO_SCENARIO_ID,
  DEMO_SCENARIOS,
  demoScenario,
  isDemoScenarioId,
} from "../demoScenarios";
import {
  DEFAULT_DEMO_EVENT_TYPE,
  DEMO_EVENT_TYPES,
  RECEIVER_LAB_BASE_URL,
  demoEventDefinition,
  demoEventPayload,
  demoEventReference,
  initialDemoEventValues,
  isCanonicalDemoEndpoint,
  isDemoEventTypeId,
  isExpectedSchemaRejection,
} from "../demoEvents";
import { shortId } from "../format";
import {
  runReliabilityTour,
  type ReliabilityTourProgress,
  type ReliabilityTourResult,
} from "../reliabilityTour";
import type {
  Delivery,
  DemoEventPayload,
  DemoEventTypeId,
  DemoScenarioId,
  EventAccepted,
} from "../types";

const STORY_EVENT_KEY = "eventharbor.control-room.event-id";
const STORY_TOUR_KEY = "eventharbor.control-room.tour";
const STORY_MODE_KEY = "eventharbor.control-room.mode";
const LOCAL_MAX_ATTEMPTS = 4;

type DemoMode = "guided" | "operator";

interface TourStart {
  runId: string;
  eventId?: string;
  replayDeliveryId?: string;
  payload: DemoEventPayload;
  scenarioId: DemoScenarioId;
}

interface EventDraft {
  type: DemoEventTypeId;
  values: Record<string, string>;
}

function draftFromPayload(payload: DemoEventPayload): EventDraft {
  const definition = demoEventDefinition(payload.type);
  return {
    type: payload.type,
    values: Object.fromEntries(definition.fields.map((field) => {
      const value = payload.data[field.key];
      if (field.kind === "money" && typeof value === "number") {
        return [field.key, (value / 100).toFixed(2)];
      }
      return [field.key, value === undefined ? "" : String(value)];
    })),
  };
}

function initialEventDraft(type: DemoEventTypeId = DEFAULT_DEMO_EVENT_TYPE): EventDraft {
  return { type, values: initialDemoEventValues(type) };
}

function payloadFromDraft(draft: EventDraft): DemoEventPayload {
  return demoEventPayload(draft.type, draft.values);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function storedPayload(value: unknown): DemoEventPayload | null {
  if (isRecord(value) && isDemoEventTypeId(value.type) && isRecord(value.data)) {
    const validValues = Object.values(value.data).every(
      (item) => typeof item === "string" || typeof item === "number",
    );
    if (validValues) {
      return {
        type: value.type,
        data: value.data as Record<string, string | number>,
      };
    }
  }

  // Migrate a session created by the earlier single-order composer.
  if (
    isRecord(value) &&
    typeof value.order_id === "string" &&
    typeof value.amount_cents === "number"
  ) {
    return {
      type: DEFAULT_DEMO_EVENT_TYPE,
      data: {
        order_id: value.order_id,
        customer_id: "CUS-MIGRATED",
        amount_cents: value.amount_cents,
        currency: "USD",
      },
    };
  }
  return null;
}

function storedTourStart(): TourStart | null {
  try {
    const raw = sessionStorage.getItem(STORY_TOUR_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<TourStart>;
    const payload = storedPayload(value.payload);
    if (
      typeof value.runId !== "string" ||
      payload === null ||
      (value.eventId !== undefined && typeof value.eventId !== "string") ||
      (value.replayDeliveryId !== undefined && typeof value.replayDeliveryId !== "string")
    ) return null;
    return {
      ...value,
      payload,
      scenarioId: isDemoScenarioId(value.scenarioId)
        ? value.scenarioId
        : DEFAULT_DEMO_SCENARIO_ID,
    } as TourStart;
  } catch {
    return null;
  }
}

function rememberTourStart(tour: TourStart | null): void {
  try {
    if (tour) sessionStorage.setItem(STORY_TOUR_KEY, JSON.stringify(tour));
    else sessionStorage.removeItem(STORY_TOUR_KEY);
  } catch {
    // Reload recovery is optional when browser storage is disabled.
  }
}

function storedStoryMode(): DemoMode | null {
  try {
    const value = sessionStorage.getItem(STORY_MODE_KEY);
    return value === "guided" || value === "operator" ? value : null;
  } catch {
    return null;
  }
}

function rememberStoryMode(mode: DemoMode | null): void {
  try {
    if (mode) sessionStorage.setItem(STORY_MODE_KEY, mode);
    else sessionStorage.removeItem(STORY_MODE_KEY);
  } catch {
    // Actor provenance is best-effort when browser storage is disabled.
  }
}

function storedStoryEvent(): string | null {
  try {
    return sessionStorage.getItem(STORY_EVENT_KEY);
  } catch {
    return null;
  }
}

function rememberStoryEvent(eventId: string | null): void {
  try {
    if (eventId) sessionStorage.setItem(STORY_EVENT_KEY, eventId);
    else sessionStorage.removeItem(STORY_EVENT_KEY);
  } catch {
    // The live journey still works when browser storage is disabled.
  }
}

function latestDelivery(deliveries: Delivery[] | undefined): Delivery | undefined {
  return deliveries?.reduce<Delivery | undefined>((latest, delivery) => {
    if (!latest || delivery.replay_generation > latest.replay_generation) return delivery;
    return latest;
  }, undefined);
}

function deliveryAtGeneration(deliveries: Delivery[] | undefined, generation: number): Delivery | undefined {
  return deliveries?.find((delivery) => delivery.replay_generation === generation);
}

function waitWithAbort(durationMs: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new Error("The reliability tour was paused."));
      return;
    }
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new Error("The reliability tour was paused."));
    };
    const timer = window.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, durationMs);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export function ControlRoomExperience() {
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedEventType = searchParams.get("event_type");
  const startFreshEvent = searchParams.get("new_event") === "1";
  const initialEventType = isDemoEventTypeId(requestedEventType)
    ? requestedEventType
    : DEFAULT_DEMO_EVENT_TYPE;
  const initialTour = startFreshEvent ? null : storedTourStart();
  const queryClient = useQueryClient();
  const tourAbort = useRef<AbortController | null>(null);
  const [mode, setMode] = useState<DemoMode>(() => startFreshEvent ? "guided" : storedStoryMode() ?? "guided");
  const [storyMode, setStoryMode] = useState<DemoMode | null>(() => startFreshEvent ? null : storedStoryMode());
  const [draftScenarioId, setDraftScenarioId] = useState<DemoScenarioId>(
    () => initialTour?.scenarioId ?? DEFAULT_DEMO_SCENARIO_ID,
  );
  const [restoredTour, setRestoredTour] = useState<TourStart | null>(initialTour);
  const [eventDraft, setEventDraft] = useState<EventDraft>(
    () => initialTour ? draftFromPayload(initialTour.payload) : initialEventDraft(initialEventType),
  );
  const [storyEventId, setStoryEventId] = useState<string | null>(() => startFreshEvent ? null : storedStoryEvent());
  const [tourProgress, setTourProgress] = useState<ReliabilityTourProgress | null>(null);
  const [replayOpen, setReplayOpen] = useState(false);
  const [preparingNext, setPreparingNext] = useState(false);
  const draftScenario = demoScenario(draftScenarioId);
  const draftDefinition = demoEventDefinition(eventDraft.type);

  useEffect(() => {
    if (!startFreshEvent) return;
    rememberTourStart(null);
    rememberStoryEvent(null);
    rememberStoryMode(null);
    const consumedParams = new URLSearchParams(searchParams);
    consumedParams.delete("new_event");
    setSearchParams(consumedParams, { replace: true });
  }, [searchParams, setSearchParams, startFreshEvent]);

  const story = useQuery({
    queryKey: ["event", storyEventId],
    queryFn: () => getEvent(storyEventId!),
    enabled: Boolean(storyEventId),
    refetchInterval: (query) => {
      const current = latestDelivery(query.state.data?.deliveries);
      const storedScenarioId = query.state.data?.data.scenario;
      const queryScenario = demoScenario(
        isDemoScenarioId(storedScenarioId)
          ? storedScenarioId
          : restoredTour?.scenarioId ?? DEFAULT_DEMO_SCENARIO_ID,
      );
      if (current?.status === "delivered") return false;
      if (current?.status === "dead_lettered" && ((storyMode ?? mode) === "operator" || queryScenario.strategy === "terminal")) return false;
      return 700;
    },
  });
  const storedStoryScenarioId = story.data?.data.scenario;
  const activeScenarioId = isDemoScenarioId(storedStoryScenarioId)
    ? storedStoryScenarioId
    : restoredTour?.scenarioId ?? draftScenarioId;
  const activeScenario = demoScenario(activeScenarioId);
  const storyRunId = typeof story.data?.data.run_id === "string" ? story.data.data.run_id : undefined;
  const activeRunId = restoredTour?.runId ?? storyRunId;
  const receiver = useQuery({
    queryKey: ["receiver-lab", activeRunId ?? "inactive"],
    queryFn: () => getReceiverLab(activeRunId),
    enabled: Boolean(activeRunId),
    refetchInterval: activeRunId ? 600 : false,
  });

  const sourceDelivery = deliveryAtGeneration(story.data?.deliveries, 0);
  const replayGeneration = deliveryAtGeneration(story.data?.deliveries, 1);
  const currentDelivery = latestDelivery(story.data?.deliveries);
  const endpointEvidence = useQuery({
    queryKey: ["endpoint", sourceDelivery?.endpoint_id],
    queryFn: () => getEndpoint(sourceDelivery!.endpoint_id),
    enabled: Boolean(sourceDelivery),
  });
  const sourceAttempts = useQuery({
    queryKey: ["delivery-attempts", sourceDelivery?.id],
    queryFn: () => getDeliveryAttempts(sourceDelivery!.id),
    enabled: Boolean(sourceDelivery),
    refetchInterval: sourceDelivery && !["dead_lettered", "delivered"].includes(sourceDelivery.status) ? 450 : false,
  });
  const replayAttempts = useQuery({
    queryKey: ["delivery-attempts", replayGeneration?.id],
    queryFn: () => getDeliveryAttempts(replayGeneration!.id),
    enabled: Boolean(replayGeneration),
    refetchInterval: replayGeneration && !["dead_lettered", "delivered"].includes(replayGeneration.status) ? 450 : false,
  });

  const terminalSchemaVerified = Boolean(
    activeScenario.strategy === "terminal" &&
    isExpectedSchemaRejection(
      story.data?.type,
      sourceDelivery,
      sourceAttempts.data?.attempts ?? [],
    ),
  );
  const terminalEvidenceNeedsReview = Boolean(
    activeScenario.strategy === "terminal" &&
    sourceDelivery?.status === "dead_lettered" &&
    !sourceAttempts.isPending &&
    !terminalSchemaVerified,
  );
  const storyComplete = Boolean(currentDelivery?.status === "delivered" || terminalSchemaVerified);
  const storyDeadLettered = currentDelivery?.status === "dead_lettered";
  const receiverReady = receiver.data?.preset === "success";

  const guidedTour = useMutation<ReliabilityTourResult, Error, TourStart>({
    mutationFn: async ({ runId, eventId, replayDeliveryId, payload, scenarioId: activeScenarioId }) => {
      const controller = new AbortController();
      tourAbort.current?.abort();
      tourAbort.current = controller;
      return runReliabilityTour(
        {
          getReceiverLab,
          setReceiverLabPreset,
          ensureReceiverLabEndpoint,
          publishDemoEvent,
          getEvent,
          getDeliveryAttempts,
          replayDelivery,
          wait: waitWithAbort,
          now: Date.now,
        },
        {
          runId,
          eventId,
          replayDeliveryId,
          payload,
          scenarioId: activeScenarioId,
          pollIntervalMs: 350,
          presentationPauseMs: 550,
          timeoutMs: 90_000,
          signal: controller.signal,
          onProgress: (progress) => {
            setTourProgress(progress);
            if (progress.eventId) {
              rememberStoryEvent(progress.eventId);
              setStoryEventId(progress.eventId);
            }
            const resumable: TourStart = {
              runId,
              eventId: progress.eventId ?? eventId,
              replayDeliveryId,
              payload,
              scenarioId: activeScenarioId,
            };
            rememberTourStart(resumable);
            setRestoredTour(resumable);
            if (progress.event) queryClient.setQueryData(["event", progress.event.id], progress.event);
          },
        },
      );
    },
    onSuccess: async (result, variables) => {
      setTourProgress(result);
      const resumable: TourStart = {
        runId: result.runId,
        eventId: result.eventId ?? variables.eventId,
        replayDeliveryId: result.replayDeliveryId ?? variables.replayDeliveryId,
        payload: result.payload ?? variables.payload,
        scenarioId: result.scenarioId,
      };
      rememberTourStart(resumable);
      setRestoredTour(resumable);
      if (result.event) queryClient.setQueryData(["event", result.event.id], result.event);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["receiver-lab"] }),
        queryClient.invalidateQueries({ queryKey: ["events"] }),
        queryClient.invalidateQueries({ queryKey: ["dead-letters"] }),
        queryClient.invalidateQueries({ queryKey: ["delivery-attempts"] }),
      ]);
    },
  });

  const runOperatorStory = useMutation<EventAccepted, Error, TourStart>({
    mutationFn: async (start) => {
      const scenario = demoScenario(start.scenarioId);
      await setReceiverLabPreset(scenario.preset, start.runId);
      const endpoint = await ensureReceiverLabEndpoint(start.payload.type);
      return publishDemoEvent(
        endpoint.id,
        start.runId,
        `control-room-story-${start.runId}`,
        start.payload,
        start.scenarioId,
      );
    },
    onSuccess: async (accepted, start) => {
      const active: TourStart = { ...start, eventId: accepted.event_id };
      rememberTourStart(active);
      setRestoredTour(active);
      rememberStoryEvent(accepted.event_id);
      setStoryEventId(accepted.event_id);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["receiver-lab"] }),
        queryClient.invalidateQueries({ queryKey: ["events"] }),
      ]);
    },
  });

  const restoreReceiverHealth = useMutation({
    mutationFn: async () => {
      const receiverState = await getReceiverLab(activeRunId);
      return receiverState.preset === "success"
        ? receiverState
        : setReceiverLabPreset("success", activeRunId);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["receiver-lab"] });
    },
  });

  const approveReplay = useMutation({
    mutationFn: (deliveryId: string) => replayDelivery(deliveryId),
    onSuccess: async (accepted) => {
      setReplayOpen(false);
      if (restoredTour) {
        const resumable = { ...restoredTour, replayDeliveryId: accepted.delivery_id };
        rememberTourStart(resumable);
        setRestoredTour(resumable);
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["event", storyEventId] }),
        queryClient.invalidateQueries({ queryKey: ["dead-letters"] }),
      ]);
    },
  });

  useEffect(() => () => tourAbort.current?.abort(), []);

  function clearBrowserStory(): void {
    tourAbort.current?.abort();
    rememberStoryEvent(null);
    rememberTourStart(null);
    rememberStoryMode(null);
    setRestoredTour(null);
    setStoryMode(null);
    setStoryEventId(null);
    setTourProgress(null);
    setReplayOpen(false);
    setPreparingNext(false);
    guidedTour.reset();
    runOperatorStory.reset();
    restoreReceiverHealth.reset();
    approveReplay.reset();
  }

  function startGuidedTour(): void {
    if (guidedTour.isPending) return;
    const next: TourStart = {
      runId: crypto.randomUUID(),
      payload: payloadFromDraft(eventDraft),
      scenarioId: draftScenarioId,
    };
    clearBrowserStory();
    setMode("guided");
    setStoryMode("guided");
    rememberStoryMode("guided");
    rememberTourStart(next);
    setRestoredTour(next);
    guidedTour.mutate(next);
    window.requestAnimationFrame(() => {
      document.getElementById("live-proof")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  function resumeGuidedTour(): void {
    const prior = guidedTour.data;
    if (!prior && !restoredTour) return;
    guidedTour.mutate({
      runId: prior?.runId ?? restoredTour!.runId,
      eventId: prior?.eventId ?? restoredTour?.eventId,
      replayDeliveryId: prior?.replayDeliveryId ?? restoredTour?.replayDeliveryId,
      payload: prior?.payload ?? restoredTour?.payload ?? payloadFromDraft(eventDraft),
      scenarioId: prior?.scenarioId ?? restoredTour?.scenarioId ?? draftScenarioId,
    });
  }

  function startOperatorStory(): void {
    if (runOperatorStory.isPending) return;
    const next: TourStart = {
      runId: crypto.randomUUID(),
      payload: payloadFromDraft(eventDraft),
      scenarioId: draftScenarioId,
    };
    clearBrowserStory();
    setMode("operator");
    setStoryMode("operator");
    rememberStoryMode("operator");
    rememberTourStart(next);
    setRestoredTour(next);
    runOperatorStory.mutate(next);
    window.requestAnimationFrame(() => {
      document.getElementById("live-proof")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  function prepareAnother(): void {
    const currentIndex = DEMO_SCENARIOS.findIndex((scenario) => scenario.id === activeScenario.id);
    const nextScenario = DEMO_SCENARIOS[(currentIndex + 1) % DEMO_SCENARIOS.length];
    if (nextScenario) setDraftScenarioId(nextScenario.id);
    setEventDraft(initialEventDraft(eventDraft.type));
    setPreparingNext(true);
    window.requestAnimationFrame(() => {
      document.getElementById("demo-input")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  const currentReceiverObservations = receiver.data?.requests.filter(
    (observation) => observation.event_id === storyEventId,
  ) ?? [];
  const destinationRepaired = Boolean(
    sourceDelivery?.status === "dead_lettered" &&
    (
      replayGeneration ||
      receiverReady ||
      tourProgress?.phase === "repairing" ||
      currentReceiverObservations.some((observation) => observation.response_status_code === 200)
    ),
  );
  const receiverControlStatus = !activeRunId
    ? "idle"
    : receiver.isPending
      ? "loading"
      : receiver.isError
        ? "unavailable"
        : "ready";
  const eventDraftValid = draftDefinition.fields.every((field) => {
    if (
      draftScenario.strategy === "terminal" &&
      field.key === draftDefinition.invalidField
    ) return true;
    const value = eventDraft.values[field.key] ?? "";
    if (field.kind === "money") {
      const amount = Number.parseFloat(value);
      return Number.isFinite(amount) && amount > 0;
    }
    if (field.kind === "integer") {
      const number = Number(value);
      return Number.isInteger(number) && number > 0;
    }
    if (field.key === "currency") return value.trim().length === 3;
    return value.trim().length > 0;
  });
  const restoredCanResume = Boolean(
    restoredTour &&
    !guidedTour.isPending &&
    (restoredTour.eventId ? story.data && !storyComplete : !storyEventId),
  );
  const guidedCanResume = guidedTour.data?.phase === "paused" || restoredCanResume;
  const guidedFailed = guidedTour.data?.phase === "failed";
  const activeMessage = tourProgress?.message
    ?? (storyEventId
      ? "Reading durable state and independent receiver evidence."
      : "Ready. The live journey will update only from observed system state.");
  const journeyStarted = Boolean(
    guidedTour.isPending ||
    runOperatorStory.isPending ||
    storyEventId ||
    restoredTour,
  );
  const journeyLoading = Boolean(
    guidedTour.isPending ||
    runOperatorStory.isPending ||
    (storyEventId && !story.data),
  );
  const activeMode = storyMode ?? mode;
  const runFailed = guidedFailed || guidedTour.isError || runOperatorStory.isError || terminalEvidenceNeedsReview;
  const formLocked = journeyStarted && !storyComplete && !preparingNext && !runFailed;
  const activeEventType = isDemoEventTypeId(story.data?.type)
    ? story.data.type
    : restoredTour?.payload.type ?? eventDraft.type;
  const activeDefinition = demoEventDefinition(activeEventType);
  const activePayload: DemoEventPayload = {
    type: activeEventType,
    data: story.data?.data as Record<string, string | number> ?? restoredTour?.payload.data ?? {},
  };
  const activeReference = story.data || restoredTour
    ? demoEventReference(activePayload)
    : "Accepting event…";
  const visibleEndpointName = endpointEvidence.data
    ? isCanonicalDemoEndpoint(endpointEvidence.data.url)
      ? endpointEvidence.data.name
      : endpointEvidence.data.url === RECEIVER_LAB_BASE_URL
        ? "Receiver Lab · legacy generic route"
        : endpointEvidence.data.name
    : activeDefinition.destinationName;
  const journeyActionMessage = storyComplete
    ? "This completed event stays on screen while you prepare the next failure scenario above."
    : activeMessage;

  return (
    <>
      <section className="control-hero" id="demo-input" data-running={formLocked}>
        <div className="hero-copy">
          <p className="eyebrow">Live webhook journey · real API, database, and HTTP</p>
          <h1>Watch EventHarbor save, retry, and recover one webhook.</h1>
          <p className="lede">
            Choose a business event and a real Receiver Lab route. EventHarbor stores it in PostgreSQL before
            a background worker sends the webhook. The receiver&apos;s actual HTTP response drives every status you see.
          </p>
          <a className="hero-demo-direction" href="#interactive-demo">
            <span>
              <small>Try it yourself</small>
              <strong className="hero-demo-direction__wide">The interactive demo is on the right</strong>
              <strong className="hero-demo-direction__narrow">The interactive demo continues below</strong>
            </span>
            <i aria-hidden="true">→</i>
          </a>
          <div className="plain-flow" aria-label="The event moves from this browser through EventHarbor to Receiver Lab">
            <span>Browser UI</span><i aria-hidden="true">→</i><strong>Store + deliver</strong><i aria-hidden="true">→</i><span>Receiver Lab</span>
          </div>
          <ul className="hero-trust-strip" aria-label="Live demonstration guarantees">
            <li>Real API call</li><li>Stored before delivery</li><li>Separate receiver service</li><li>Independent receipts</li>
          </ul>
        </div>

        <form
          id="interactive-demo"
          className="scenario-panel event-composer"
          aria-labelledby="event-composer-title"
          onSubmit={(event) => {
            event.preventDefault();
            if (formLocked) return;
            if (mode === "guided") startGuidedTour();
            else startOperatorStory();
          }}
        >
          <div className="composer-topline">
            <span className="section-index">1 / Choose how the receiver behaves</span>
            <div className="composer-mode-switch" role="group" aria-label="Demo mode">
              <button type="button" disabled={formLocked} aria-label="Guided run: automatically demonstrate failure and recovery" aria-pressed={mode === "guided"} onClick={() => setMode("guided")}>Guided run</button>
              <button type="button" disabled={formLocked} aria-label="Manual controls: you restore receiver health and approve replay" aria-pressed={mode === "operator"} onClick={() => setMode("operator")}>Manual</button>
            </div>
          </div>
          <h2 id="event-composer-title">Choose the failure. Then send the event.</h2>

          {journeyStarted ? (
            <div className="composer-current-story" data-complete={storyComplete}>
              <span>{runFailed ? "Previous attempt preserved below" : storyComplete ? "Completed story preserved below" : "Current story running below"}</span>
              <strong>{activeReference}</strong>
              <small>{storyComplete || runFailed ? "You can prepare the next event without removing this result." : "Finish or pause this event before replacing it."}</small>
            </div>
          ) : null}

          <div className="scenario-picker-intro">
            <h3>Test cases</h3>
            <p>Choose one controlled receiver behavior. Each case produces real HTTP responses and stored delivery evidence.</p>
          </div>
          <div className="scenario-picker" role="group" aria-label="Receiver incident">
            {DEMO_SCENARIOS.map((scenario) => (
              <button
                type="button"
                disabled={formLocked}
                aria-pressed={scenario.id === draftScenarioId}
                className="scenario-choice"
                key={scenario.id}
                onClick={() => setDraftScenarioId(scenario.id)}
              >
                <strong>{scenario.label}</strong>
                <span>{scenario.sequence.map((step) => step.toUpperCase()).join(" → ")}</span>
              </button>
            ))}
          </div>

          <fieldset className="event-composer__payload" disabled={formLocked}>
            <label className="event-composer__type">
              <span>Business event type</span>
              <select
                value={eventDraft.type}
                onChange={(event) => {
                  const nextType = event.target.value;
                  if (isDemoEventTypeId(nextType)) setEventDraft(initialEventDraft(nextType));
                }}
              >
                {DEMO_EVENT_TYPES.map((definition) => (
                  <option key={definition.type} value={definition.type}>{definition.type} · {definition.label}</option>
                ))}
              </select>
              <small>{draftDefinition.description}</small>
            </label>

            <div className="event-composer__destination">
              <span>Real destination selected by this event type</span>
              <strong>{draftDefinition.destinationName}</strong>
              <code>{draftDefinition.destinationUrl}</code>
              <small>{draftDefinition.destinationPurpose} These are distinct routes in one separate FastAPI service.</small>
            </div>

            <div className="event-composer__fields">
              {draftDefinition.fields.map((field) => {
                const omitted = draftScenario.strategy === "terminal" && field.key === draftDefinition.invalidField;
                if (omitted) {
                  return (
                    <div className="event-field-omitted" key={field.key}>
                      <span>{field.label}</span>
                      <strong>Intentionally omitted from the JSON body</strong>
                      <code>data.{field.key}</code>
                      <small>{draftDefinition.receiverRequirement}</small>
                    </div>
                  );
                }
                const value = eventDraft.values[field.key] ?? "";
                const update = (nextValue: string) => setEventDraft((current) => ({
                  ...current,
                  values: { ...current.values, [field.key]: nextValue },
                }));
                return (
                  <label key={field.key}>
                    <span>{field.label}</span>
                    {field.kind === "money" ? (
                      <span className="event-composer__money">
                        <i aria-hidden="true">$</i>
                        <input type="number" min="0.01" step="0.01" value={value} onChange={(event) => update(event.target.value)} />
                      </span>
                    ) : (
                      <input
                        type={field.kind === "integer" ? "number" : "text"}
                        min={field.kind === "integer" ? 1 : undefined}
                        step={field.kind === "integer" ? 1 : undefined}
                        maxLength={field.kind === "text" ? 80 : undefined}
                        value={value}
                        onChange={(event) => update(event.target.value)}
                      />
                    )}
                    <small>{field.help}</small>
                  </label>
                );
              })}
            </div>
          </fieldset>

          <div className="receiver-contract" data-terminal={draftScenario.strategy === "terminal"}>
            <span>{draftScenario.strategy === "terminal" ? "Why this request will be rejected" : "What the real receiver will do"}</span>
            {draftScenario.strategy === "terminal" ? (
              <>
                <strong>The browser sends <code>{eventDraft.type}</code> without <code>data.{draftDefinition.invalidField}</code>.</strong>
                <ol className="receiver-decision-list">
                  <li><b>EventHarbor intake · HTTP 202</b><small>The event envelope is valid, so it is stored durably before delivery.</small></li>
                  <li><b>Receiver Lab · HTTP 400</b><small>Its {eventDraft.type} contract requires {draftDefinition.invalidField}. Retrying the unchanged body cannot fix it.</small></li>
                </ol>
              </>
            ) : (
              <><strong>{draftScenario.contract}</strong><p><b>Concrete cause</b>{draftScenario.cause}</p></>
            )}
            <small>{draftScenario.takeaway}</small>
          </div>
          <button className="story-action" type="submit" disabled={formLocked || !eventDraftValid}>
            <span>{formLocked
              ? "Current event is still running"
              : draftScenario.strategy === "terminal"
                ? `Send without ${draftDefinition.invalidField}`
                : journeyStarted ? "Send this next event" : "Send this event and watch it move"}</span><span aria-hidden="true">→</span>
          </button>
          <small>Synthetic business data. Real API transaction, PostgreSQL state, worker requests, HTTP responses, and receiver receipts.</small>
        </form>
      </section>

      {journeyStarted ? (
        <section className="active-run-banner" aria-label="Active demonstration">
          <div><p className="eyebrow">Following this event</p><h1>{activeReference}</h1></div>
          <dl>
            <div><dt>Event type</dt><dd><code>{activeEventType}</code></dd></div>
            <div><dt>Destination</dt><dd>{visibleEndpointName}</dd></div>
            <div><dt>Event</dt><dd><code>{storyEventId ? shortId(storyEventId) : "Accepting…"}</code></dd></div>
          </dl>
        </section>
      ) : null}

      <EventJourney
        scenario={activeScenario}
        demoMode={activeMode}
        tourPhase={tourProgress?.phase ?? null}
        tourMessage={journeyActionMessage}
        isStarting={journeyLoading}
        runId={activeRunId}
        event={story.data ?? null}
        endpointName={visibleEndpointName}
        endpointUrl={endpointEvidence.data?.url ?? activeDefinition.destinationUrl}
        originalDelivery={sourceDelivery ?? null}
        replayDelivery={replayGeneration ?? null}
        originalAttempts={sourceAttempts.data?.attempts ?? []}
        replayAttempts={replayAttempts.data?.attempts ?? []}
        receiverObservations={currentReceiverObservations}
        receiverControlStatus={receiverControlStatus}
        repairOccurred={destinationRepaired}
        replayOccurred={Boolean(replayGeneration)}
        maxAttempts={LOCAL_MAX_ATTEMPTS}
      >
        {journeyStarted ? (
          <>
            {activeMode === "guided" ? (
              <>
                {guidedTour.isPending ? (
                  <><button className="story-action" type="button" disabled><span>Following live system state…</span><span aria-hidden="true">●</span></button><button className="reliability-abandon" type="button" onClick={() => tourAbort.current?.abort()}>Pause demo</button></>
                ) : null}
                {guidedCanResume ? <button className="story-action" type="button" onClick={resumeGuidedTour}><span>Resume from stored evidence</span><span aria-hidden="true">→</span></button> : null}
                {guidedFailed ? <button className="story-action story-action-repair" type="button" onClick={prepareAnother}><span>Prepare a fresh event</span><span aria-hidden="true">↻</span></button> : null}
                {storyComplete && !guidedTour.isPending ? <button className="story-action story-action-complete" type="button" onClick={prepareAnother}><span>Set up another failure scenario</span><span aria-hidden="true">↗</span></button> : null}
              </>
            ) : (
              <>
                {runOperatorStory.isPending || (storyEventId && !storyDeadLettered && !storyComplete) ? <button className="story-action" type="button" disabled><span>Following live system state…</span><span aria-hidden="true">●</span></button> : null}
                {storyDeadLettered && activeScenario.strategy === "replay" && !receiverReady ? (
                  <>
                    <div className="recovery-action-explainer">
                      <span>Restore the destination&apos;s health</span>
                      <strong>Only this test receiver changes: HTTP 503 unavailable → HTTP 200 accepting</strong>
                      <p>The service is reachable but currently unavailable. This simulates it becoming healthy after a restart or deployment. EventHarbor has already made four automatic attempts and saved every failure; this control does not erase or resend anything.</p>
                    </div>
                    <button className="story-action story-action-repair" type="button" disabled={restoreReceiverHealth.isPending} onClick={() => restoreReceiverHealth.mutate()}><span>{restoreReceiverHealth.isPending ? "Restoring test receiver health…" : "Restore test receiver health"}</span><span aria-hidden="true">→</span></button>
                  </>
                ) : null}
                {storyDeadLettered && activeScenario.strategy === "replay" && receiverReady && sourceDelivery && !replayGeneration ? (
                  <>
                    <div className="recovery-action-explainer" data-restored="true">
                      <span>Receiver healthy · ready for HTTP 200</span>
                      <strong>The original event is still stopped, saved, and unchanged.</strong>
                      <p>EventHarbor does not continue indefinitely after the retry budget. Operator approval controls when the stopped, preserved event is sent again and makes the recovery deliberate and traceable.</p>
                    </div>
                    <button className="story-action" type="button" onClick={() => setReplayOpen(true)}><span>Replay the preserved event with EventHarbor</span><span aria-hidden="true">→</span></button>
                  </>
                ) : null}
                {storyComplete ? <button className="story-action story-action-complete" type="button" onClick={prepareAnother}><span>Set up another failure scenario</span><span aria-hidden="true">↗</span></button> : null}
              </>
            )}
            {guidedTour.isError ? <ErrorState error={guidedTour.error} /> : null}
            {runOperatorStory.isError ? <ErrorState error={runOperatorStory.error} /> : null}
            {restoreReceiverHealth.isError ? <ErrorState error={restoreReceiverHealth.error} /> : null}
            {approveReplay.isError ? <ErrorState error={approveReplay.error} /> : null}
            {story.isError && storyEventId ? <ErrorState error={story.error} retry={() => void story.refetch()} /> : null}
          </>
        ) : null}
      </EventJourney>

      {story.data ? (
        <details className="technical-evidence journey-postgres-lineage">
          <summary>View PostgreSQL delivery lineage</summary>
          <EventTimeline deliveries={story.data.deliveries} />
        </details>
      ) : null}

      {sourceDelivery ? (
        <ReplayConfirmation
          open={replayOpen}
          deliveryId={sourceDelivery.id}
          busy={approveReplay.isPending}
          onCancel={() => setReplayOpen(false)}
          onConfirm={() => approveReplay.mutate(sourceDelivery.id)}
        />
      ) : null}
      {storyEventId ? <Link className="event-reference control-room-event-link" to={`/events/${storyEventId}`}>Open the complete event record / {shortId(storyEventId)} →</Link> : null}
    </>
  );
}
