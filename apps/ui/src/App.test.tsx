import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { ApiError, apiClient } from "./api/client";

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("App routing", () => {
  it("renders the fixtures page at /", async () => {
    vi.spyOn(apiClient, "listFixtures").mockResolvedValue([]);
    renderAt("/");
    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 1, name: /fixtures/i })).toBeInTheDocument(),
    );
  });

  it("renders the match page at /matches/:id", async () => {
    vi.spyOn(apiClient, "getMatch").mockResolvedValue({
      id: 42,
      competition: "WC",
      season: "2026",
      home_team: "BRA",
      away_team: "ARG",
      kickoff_utc: "2026-06-11T12:00:00Z",
      status: "scheduled",
    });
    vi.spyOn(apiClient, "getMatchNote").mockRejectedValue(new ApiError(404, null, "not found"));
    renderAt("/matches/42");
    expect(await screen.findByRole("heading", { level: 1, name: /match #42/i })).toBeInTheDocument();
  });
});
