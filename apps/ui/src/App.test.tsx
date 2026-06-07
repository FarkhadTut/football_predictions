import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { apiClient } from "./api/client";

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

  it("renders the match placeholder at /matches/:id", () => {
    renderAt("/matches/42");
    expect(screen.getByRole("heading", { level: 1, name: /match/i })).toBeInTheDocument();
  });
});
