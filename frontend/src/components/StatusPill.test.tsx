import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusPill } from "./StatusPill";

describe("StatusPill", () => {
  it("communicates state with text, not color alone", () => {
    render(<StatusPill status="dead_lettered" />);
    expect(screen.getByText("Stopped · saved")).toHaveClass("status-dead_lettered");
  });
});
