import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { ProofReceipt, StoryProgress } from "./ReliabilityProof";

describe("StoryProgress", () => {
  it("marks exactly one of the six ordered steps as current", () => {
    render(<StoryProgress current="dead_lettered" />);

    const progress = screen.getByRole("navigation", { name: /recovery demonstration progress/i });
    const steps = within(progress).getAllByRole("listitem");

    expect(steps).toHaveLength(6);
    expect(within(progress).getByText("Stopped safely").closest("li")).toHaveAttribute(
      "aria-current",
      "step",
    );
    expect(progress.querySelectorAll('[aria-current="step"]')).toHaveLength(1);
  });
});

describe("ProofReceipt", () => {
  it("summarizes preserved failure evidence and links to the event", () => {
    render(
      <MemoryRouter>
        <ProofReceipt
          eventId="event-123"
          failedAttempts={4}
          retryStoppedSafely
          originalGenerationPreserved
          replayGeneration={1}
        />
      </MemoryRouter>,
    );

    expect(screen.getByText("4 × HTTP 503")).toBeVisible();
    expect(screen.getByText("Generation 1")).toBeVisible();
    expect(screen.getByText("HTTP 200")).toBeVisible();
    expect(screen.getByText(/2 delivery generations · 5 persisted HTTP attempts/i)).toBeVisible();
    expect(screen.getByRole("link", { name: /inspect full technical evidence/i })).toHaveAttribute(
      "href",
      "/events/event-123",
    );
  });
});
