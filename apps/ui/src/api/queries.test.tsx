import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiClient, type Fixture } from "./client";
import { useFixtures, useMatchNote } from "./queries";

function wrapper(client: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

function freshClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useFixtures", () => {
  it("delegates to apiClient.listFixtures and exposes the data", async () => {
    const fixtures: Fixture[] = [
      {
        id: 1,
        competition: "WC",
        season: "2026",
        home_team: "BRA",
        away_team: "ARG",
        kickoff_utc: "2026-06-11T12:00:00Z",
        status: "scheduled",
      },
    ];
    const spy = vi.spyOn(apiClient, "listFixtures").mockResolvedValue(fixtures);

    const { result } = renderHook(() => useFixtures(), { wrapper: wrapper(freshClient()) });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(fixtures);
    expect(spy).toHaveBeenCalledTimes(1);
  });
});

describe("useMatchNote", () => {
  it("surfaces API errors so consumers can render fallbacks", async () => {
    vi.spyOn(apiClient, "getMatchNote").mockRejectedValue(new Error("404"));

    const { result } = renderHook(() => useMatchNote(42), { wrapper: wrapper(freshClient()) });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.message).toBe("404");
  });
});
