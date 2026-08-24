import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { App } from "./App";

vi.mock("./api", async (importOriginal) => {
  const original = await importOriginal<typeof import("./api")>();
  return {
    ...original,
    getOverview: vi.fn().mockResolvedValue({
      generated_at: "2026-08-24T12:00:00Z",
      events_total: 0,
      endpoints_total: 0,
      endpoints_enabled: 0,
      deliveries: { pending: 0, in_progress: 0, retry_wait: 0, delivered: 0, dead_lettered: 0 },
      actionable_dead_letters: 0,
    }),
  };
});

describe("application routes", () => {
  it("renders a useful fallback for an unknown route", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/does-not-exist"]}>
          <App />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    expect(screen.getByRole("heading", { name: "Nothing is routed here." })).toBeVisible();
    expect(screen.getByRole("link", { name: /return to control room/i })).toHaveAttribute("href", "/");
  });
});

