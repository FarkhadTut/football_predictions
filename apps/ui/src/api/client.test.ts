import { describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError, isCachedPrediction, type PredictResponse } from "./client";

function buildClient(fetchMock: typeof fetch) {
  return new ApiClient({ baseUrl: "http://api.test", fetch: fetchMock });
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("ApiClient", () => {
  it("listFixtures issues a GET against /fixtures and parses the body", async () => {
    const fixture = {
      id: 1,
      competition: "WC",
      season: "2026",
      home_team: "BRA",
      away_team: "ARG",
      kickoff_utc: "2026-06-11T12:00:00Z",
      status: "scheduled",
    };
    const fetchMock = vi.fn(async () => jsonResponse([fixture]));
    const client = buildClient(fetchMock as unknown as typeof fetch);

    const result = await client.listFixtures();

    expect(result).toEqual([fixture]);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/fixtures",
      expect.objectContaining({ headers: expect.objectContaining({ Accept: "application/json" }) }),
    );
  });

  it("predict POSTs with the JSON body and Content-Type", async () => {
    const responseBody: PredictResponse = {
      cached: false,
      model_run_id: 9,
      status: "running",
    };
    const fetchMock = vi.fn(async () => jsonResponse(responseBody, 202));
    const client = buildClient(fetchMock as unknown as typeof fetch);

    const result = await client.predict(7, { force_refit: true });

    expect(isCachedPrediction(result)).toBe(false);
    const call = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(call[0]).toBe("http://api.test/matches/7/predict");
    const init = call[1];
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ force_refit: true }));
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("throws ApiError on non-2xx with status + parsed body", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ detail: "not found" }, 404));
    const client = buildClient(fetchMock as unknown as typeof fetch);

    await expect(client.getMatch(123)).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      body: { detail: "not found" },
    });
  });

  it("ApiError preserves status and body for callers that catch it", async () => {
    const err = new ApiError(422, { errors: [{ field: "x" }] }, "boom");
    expect(err.status).toBe(422);
    expect(err.body).toEqual({ errors: [{ field: "x" }] });
  });

  it("notesEventsUrl returns the SSE URL relative to baseUrl", () => {
    const client = buildClient(vi.fn() as unknown as typeof fetch);
    expect(client.notesEventsUrl()).toBe("http://api.test/events/notes");
  });
});
