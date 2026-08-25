import { Link } from "react-router-dom";

export type ReliabilityStoryPhase =
  | "ready"
  | "accepted"
  | "retrying"
  | "dead_lettered"
  | "replaying"
  | "verified";

interface StoryProgressProps {
  current: ReliabilityStoryPhase;
}

interface DemoNarrativeProps {
  phase: ReliabilityStoryPhase;
  eventLabel?: string;
  attemptNumber?: number;
  maximumAttempts?: number;
}

interface ProofReceiptProps {
  eventId: string;
  eventLabel?: string;
  failedAttempts: number;
  retryStoppedSafely: boolean;
  originalGenerationPreserved: boolean;
  replayGeneration: number;
  failedHttpStatus?: number;
  recoveredHttpStatus?: number;
}

const STORY_STEPS: ReadonlyArray<{
  id: ReliabilityStoryPhase;
  label: string;
  explanation: string;
}> = [
  {
    id: "ready",
    label: "Ready",
    explanation: "Prepare one real event and a controlled receiver failure.",
  },
  {
    id: "accepted",
    label: "Saved first",
    explanation: "Commit the event before attempting network delivery.",
  },
  {
    id: "retrying",
    label: "Receiver down",
    explanation: "Record each failed HTTP request and retry with limits.",
  },
  {
    id: "dead_lettered",
    label: "Stopped safely",
    explanation: "End retries at an inspectable failure boundary.",
  },
  {
    id: "replaying",
    label: "Controlled recovery",
    explanation: "Restore receiver health and append a replay delivery.",
  },
  {
    id: "verified",
    label: "Verified",
    explanation: "Confirm recovery without erasing the original failure.",
  },
];

const NARRATIVE_COPY: Record<
  ReliabilityStoryPhase,
  { index: string; eyebrow: string; title: string }
> = {
  ready: {
    index: "01",
    eyebrow: "Ready",
    title: "A customer system is about to become unavailable.",
  },
  accepted: {
    index: "02",
    eyebrow: "Saved first",
    title: "The message is safe before delivery begins.",
  },
  retrying: {
    index: "03",
    eyebrow: "Receiver down",
    title: "HTTP 503. EventHarbor keeps control.",
  },
  dead_lettered: {
    index: "04",
    eyebrow: "Stopped safely",
    title: "Four failures. No infinite retry loop.",
  },
  replaying: {
    index: "05",
    eyebrow: "Controlled recovery",
    title: "Recovery creates new history, not rewritten history.",
  },
  verified: {
    index: "06",
    eyebrow: "Recovery verified",
    title: "Delivered without erasing the failure.",
  },
};

function narrativeDescription({
  phase,
  eventLabel,
  attemptNumber,
  maximumAttempts,
}: DemoNarrativeProps): string {
  const eventReference = eventLabel ? `Event ${eventLabel}` : "The event";

  switch (phase) {
    case "ready":
      return "Start one real event and watch EventHarbor protect it from durable acceptance through recovery.";
    case "accepted":
      return `${eventReference} and generation 0 were committed together in PostgreSQL.`;
    case "retrying": {
      const attempt = attemptNumber ?? 1;
      const limit = maximumAttempts ?? 4;
      return `Attempt ${attempt} of ${limit} returned HTTP 503. The receiver is unavailable, but the event and its delivery evidence are durable.`;
    }
    case "dead_lettered":
      return "The retry budget is exhausted. The delivery is now dead-lettered: a terminal, inspectable state that requires deliberate recovery.";
    case "replaying":
      return "The demo operator restored receiver health and approved a replay. The original delivery and every failed attempt remain unchanged.";
    case "verified":
      return "The replay delivered with HTTP 200. The original delivery still proves what failed, and the recovery delivery proves the healthy receiver accepted the replay.";
  }
}

function yesOrNotYet(value: boolean): string {
  return value ? "Yes" : "Not yet";
}

export function PlainEnglishFlow() {
  return (
    <section className="reliability-plain-flow" aria-labelledby="reliability-flow-title">
      <header className="reliability-plain-flow__header">
        <p className="reliability-eyebrow">Plain-English version</p>
        <h2 id="reliability-flow-title">One application needs to tell another that something happened.</h2>
        <p>
          A webhook is simply an HTTP message between applications, such as a payment service announcing
          that an invoice was paid. EventHarbor protects that message when the receiving system is unavailable.
        </p>
      </header>

      <ol className="reliability-flow" aria-label="Reliable message delivery flow">
        <li className="reliability-flow__step">
          <span className="reliability-flow__index" aria-hidden="true">01</span>
          <strong>Your application</strong>
          <p>Creates one important event.</p>
        </li>
        <li className="reliability-flow__step">
          <span className="reliability-flow__index" aria-hidden="true">02</span>
          <strong>EventHarbor</strong>
          <p>Saves it first, then signs, sends, retries, and records the evidence.</p>
        </li>
        <li className="reliability-flow__step">
          <span className="reliability-flow__index" aria-hidden="true">03</span>
          <strong>Customer system</strong>
          <p>Receives the message now, or after a controlled recovery.</p>
        </li>
      </ol>

      <p className="reliability-plain-flow__promise">
        If the customer system goes down, the event remains safe and every delivery attempt remains explainable.
      </p>
    </section>
  );
}

export function StoryProgress({ current }: StoryProgressProps) {
  const currentIndex = STORY_STEPS.findIndex((step) => step.id === current);

  return (
    <nav className="reliability-story-progress" aria-label="Live recovery demonstration progress">
      <ol className="reliability-story-progress__list">
        {STORY_STEPS.map((step, index) => {
          const state = index < currentIndex ? "complete" : index === currentIndex ? "current" : "upcoming";
          return (
            <li
              className={`reliability-story-progress__step reliability-story-progress__step--${state}`}
              key={step.id}
              aria-current={step.id === current ? "step" : undefined}
              data-state={state}
            >
              <span className="reliability-story-progress__index" aria-hidden="true">
                {String(index + 1).padStart(2, "0")}
              </span>
              <strong>{step.label}</strong>
              <span>{step.explanation}</span>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

export function DemoNarrative(props: DemoNarrativeProps) {
  const copy = NARRATIVE_COPY[props.phase];

  return (
    <section
      className={`reliability-narrative reliability-narrative--${props.phase}`}
      aria-live="polite"
      aria-atomic="true"
    >
      <p className="reliability-narrative__eyebrow">
        {copy.index} / {copy.eyebrow}
      </p>
      <h2>{copy.title}</h2>
      <p>{narrativeDescription(props)}</p>
    </section>
  );
}

export function ProofReceipt({
  eventId,
  eventLabel,
  failedAttempts,
  retryStoppedSafely,
  originalGenerationPreserved,
  replayGeneration,
  failedHttpStatus = 503,
  recoveredHttpStatus = 200,
}: ProofReceiptProps) {
  const totalAttempts = failedAttempts + (recoveredHttpStatus > 0 ? 1 : 0);
  const generationCount = replayGeneration + 1;

  return (
    <section className="proof-receipt" aria-labelledby="proof-receipt-title">
      <header className="proof-receipt__header">
        <p className="proof-eyebrow">Proof / Event {eventLabel ?? eventId}</p>
        <h2 id="proof-receipt-title">Recovered without erasing the failure.</h2>
        <p>
          One immutable event now contains both the original failure evidence and the successful recovery.
        </p>
      </header>

      <dl className="proof-receipt__facts">
        <div>
          <dt>Accepted once</dt>
          <dd>Yes</dd>
        </div>
        <div>
          <dt>Original failed attempts</dt>
          <dd>{failedAttempts} × HTTP {failedHttpStatus}</dd>
        </div>
        <div>
          <dt>Retry budget stopped safely</dt>
          <dd>{yesOrNotYet(retryStoppedSafely)}</dd>
        </div>
        <div>
          <dt>Original generation preserved</dt>
          <dd>{yesOrNotYet(originalGenerationPreserved)}</dd>
        </div>
        <div>
          <dt>Replay generation</dt>
          <dd>Generation {replayGeneration}</dd>
        </div>
        <div>
          <dt>Recovered delivery</dt>
          <dd>HTTP {recoveredHttpStatus}</dd>
        </div>
        <div>
          <dt>History rewritten</dt>
          <dd>{originalGenerationPreserved ? "No" : "Not verified"}</dd>
        </div>
      </dl>

      <p className="proof-receipt__summary">
        One immutable event · {generationCount} delivery generations · {totalAttempts} persisted HTTP attempts
      </p>

      <Link className="proof-receipt__link" to={`/events/${encodeURIComponent(eventId)}`}>
        Inspect full technical evidence <span aria-hidden="true">→</span>
      </Link>
    </section>
  );
}

export function EngineeringDecisions() {
  return (
    <section className="reliability-decisions" aria-labelledby="reliability-decisions-title">
      <header className="reliability-decisions__header">
        <p className="reliability-eyebrow">Why this design holds up</p>
        <h2 id="reliability-decisions-title">The reliability is in the invariants.</h2>
      </header>

      <ol className="reliability-decisions__list">
        <li>
          <span aria-hidden="true">01</span>
          <h3>Saved before sending</h3>
          <p>The event and its initial delivery commit in one database transaction.</p>
        </li>
        <li>
          <span aria-hidden="true">02</span>
          <h3>Evidence before network I/O</h3>
          <p>Every outbound attempt is reserved before HTTP begins, so a worker crash cannot hide the attempt.</p>
        </li>
        <li>
          <span aria-hidden="true">03</span>
          <h3>Retries have a boundary</h3>
          <p>Transient failures retry with limits; exhausted work stops in an inspectable terminal state.</p>
        </li>
        <li>
          <span aria-hidden="true">04</span>
          <h3>Replay appends history</h3>
          <p>Recovery creates a new delivery generation instead of resetting or deleting the failed one.</p>
        </li>
      </ol>
    </section>
  );
}
