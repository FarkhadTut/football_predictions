import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiClient, type ClaudeNote, type MatchDetail } from "../api/client";
import { Match } from "./Match";

function renderMatch(matchId: number) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/matches/${matchId}`]}>
        <Routes>
          <Route path="/matches/:matchId" element={<Match />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function matchStub(): MatchDetail {
  return {
    id: 7,
    competition: "WC",
    season: "2026",
    home_team: "BRA",
    away_team: "ARG",
    kickoff_utc: "2026-06-11T12:00:00Z",
    status: "scheduled",
  };
}

function noteStub(): ClaudeNote {
  return {
    match_id: 7,
    summary: "Brazil missing 2 starters; momentum shift away from home",
    confidence: 0.55,
    qualitative_deltas: [
      { market: "1x2", log_odds_shift: -0.2 },
      { market: "ou_2_5", log_odds_shift: 0.1 },
    ],
    sources: ["https://example.com/lineup"],
    created_at: "2026-06-11T10:00:00Z",
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Match page", () => {
  // TEST-011
  it("renders the three labelled panels with stub data", async () => {
    vi.spyOn(apiClient, "getMatch").mockResolvedValue(matchStub());
    vi.spyOn(apiClient, "getMatchNote").mockResolvedValue(noteStub());

    renderMatch(7);

    // All three panels in the DOM, identified by their headings.
    expect(await screen.findByRole("heading", { name: /^stats$/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /^model$/i })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /^claude note$/i })).toBeInTheDocument();

    // Stats panel shows match info once the query resolves.
    expect(await screen.findByText(/BRA vs ARG/)).toBeInTheDocument();

    // Claude panel shows the note summary.
    const claude = screen.getByRole("heading", { name: /^claude note$/i }).closest("section");
    await waitFor(() => {
      expect(within(claude!).getByText(/missing 2 starters/i)).toBeInTheDocument();
    });
  });

  // TEST-012
  it("shows the 'awaiting Claude analysis' placeholder when the note is missing", async () => {
    vi.spyOn(apiClient, "getMatch").mockResolvedValue(matchStub());
    vi.spyOn(apiClient, "getMatchNote").mockRejectedValue(new ApiError(404, null, "not found"));

    renderMatch(7);

    // Stats still populates.
    await waitFor(() => expect(screen.getByText(/BRA vs ARG/)).toBeInTheDocument());

    // Claude panel shows the placeholder once the 404 settles.
    const claude = screen.getByRole("heading", { name: /^claude note$/i }).closest("section");
    await waitFor(() => {
      expect(within(claude!).getByText(/awaiting claude analysis/i)).toBeInTheDocument();
    });
  });

  it("rejects non-numeric match ids", () => {
    renderMatch(Number.NaN);
    expect(screen.getByRole("alert")).toHaveTextContent(/invalid match id/i);
  });
});
