import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ReplayConfirmation } from "./ReplayConfirmation";

describe("ReplayConfirmation", () => {
  it("explains the side effect and requires explicit approval", () => {
    const cancel = vi.fn();
    const confirm = vi.fn();
    render(
      <ReplayConfirmation
        open
        deliveryId="01234567-89ab-cdef-0123-456789abcdef"
        busy={false}
        onCancel={cancel}
        onConfirm={confirm}
      />,
    );

    expect(screen.getByRole("alertdialog")).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText("This sends the webhook again.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /approve replay/i }));
    expect(confirm).toHaveBeenCalledOnce();
    expect(cancel).not.toHaveBeenCalled();
  });

  it("can be dismissed with Escape", () => {
    const cancel = vi.fn();
    render(
      <ReplayConfirmation
        open
        deliveryId="delivery-1"
        busy={false}
        onCancel={cancel}
        onConfirm={vi.fn()}
      />,
    );

    fireEvent.keyDown(screen.getByRole("alertdialog"), { key: "Escape" });
    expect(cancel).toHaveBeenCalledOnce();
  });
});

