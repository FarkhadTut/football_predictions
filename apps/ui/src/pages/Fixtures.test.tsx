import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiClient, type Fixture } from "../api/client";
import { Fixtures } from "./Fixtures";

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Fixtures />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function makeFixture(overrides: Partial<Fixture>): Fixture {
  return {
    id: 1,
    competition: "WC",
    season: "2026",
    home_team: "BRA",
    away_team: "ARG",
    kickoff_utc: "2026-06-11T12:00:00Z",
    status: "scheduled",
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Fixtures page", () => {
  it("renders an empty-state message when the API returns no fixtures", async () => {
    vi.spyOn(apiClient, "listFixtures").mockResolvedValue([]);
    renderPage();
    expect(await screen.findByText(/no upcoming fixtures/i)).toBeInTheDocument();
  });

  it("groups fixtures by kickoff date and links each row to /matches/:id", async () => {
    vi.spyOn(apiClient, "listFixtures").mockResolvedValue([
      // Out-of-order on purpose to confirm sorting.
      makeFixture({ id: 3, kickoff_utc: "2026-06-12T20:00:00Z", home_team: "FRA", away_team: "ESP" }),
      makeFixture({ id: 1, kickoff_utc: "2026-06-11T12:00:00Z", home_team: "BRA", away_team: "ARG" }),
      makeFixture({ id: 2, kickoff_utc: "2026-06-11T18:00:00Z", home_team: "ENG", away_team: "GER" }),
    ]);

    renderPage();

    // Two date sections, in chronological order.
    const sections = await screen.findAllByRole("article");
    expect(sections).toHaveLength(2);
    expect(within(sections[0]!).getByRole("heading", { level: 2 })).toHaveTextContent("2026-06-11");
    expect(within(sections[1]!).getByRole("heading", { level: 2 })).toHaveTextContent("2026-06-12");

    // Day 1 ordered by kickoff time within the day.
    const day1Items = within(sections[0]!).getAllByRole("listitem");
    expect(day1Items[0]).toHaveTextContent(/12:00 UTC.*BRA vs ARG/);
    expect(day1Items[1]).toHaveTextContent(/18:00 UTC.*ENG vs GER/);

    // Links target the match page.
    expect(within(sections[0]!).getByRole("link", { name: /BRA vs ARG/ })).toHaveAttribute(
      "href",
      "/matches/1",
    );
  });

  it("surfaces an alert when the fetch fails", async () => {
    vi.spyOn(apiClient, "listFixtures").mockRejectedValue(new Error("boom"));
    renderPage();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/boom/));
  });
});
