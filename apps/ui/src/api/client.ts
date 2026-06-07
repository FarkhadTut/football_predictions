/**
 * Thin typed fetch client for the predictor API (REQ-008, REQ-010).
 *
 * Types come from `types.gen.ts`, which is produced by `pnpm codegen` from
 * `packages/schemas/openapi.json`. The client exposes one helper per route
 * so call sites don't repeat URL strings or response shapes.
 */
import type { components } from "./types.gen";

export type Fixture = components["schemas"]["Fixture"];
export type MatchDetail = components["schemas"]["MatchDetail"];
export type ClaudeNote = components["schemas"]["ClaudeNote"];
export type PredictRequest = components["schemas"]["PredictRequest"];
export type PredictCachedResponse = components["schemas"]["PredictCachedResponse"];
export type PredictEnqueuedResponse = components["schemas"]["PredictEnqueuedResponse"];
export type PredictResponse = PredictCachedResponse | PredictEnqueuedResponse;

/** Discriminator on `cached` so callers can switch without `any`. */
export function isCachedPrediction(r: PredictResponse): r is PredictCachedResponse {
  return r.cached === true;
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  constructor(status: number, body: unknown, message: string) {
    super(message);
    this.status = status;
    this.body = body;
    this.name = "ApiError";
  }
}

export interface ApiClientConfig {
  /** Base URL of the FastAPI app (no trailing slash). */
  baseUrl: string;
  /** Override `fetch` (tests inject a mock). */
  fetch?: typeof fetch;
}

async function request<T>(
  config: ApiClientConfig,
  path: string,
  init?: RequestInit,
): Promise<T> {
  const doFetch = config.fetch ?? fetch;
  const resp = await doFetch(`${config.baseUrl}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body !== undefined ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  const text = await resp.text();
  const body: unknown = text ? JSON.parse(text) : null;
  if (!resp.ok) {
    throw new ApiError(resp.status, body, `${init?.method ?? "GET"} ${path} → ${resp.status}`);
  }
  return body as T;
}

export class ApiClient {
  constructor(private readonly config: ApiClientConfig) {}

  listFixtures(): Promise<Fixture[]> {
    return request<Fixture[]>(this.config, "/fixtures");
  }

  getMatch(matchId: number): Promise<MatchDetail> {
    return request<MatchDetail>(this.config, `/matches/${matchId}`);
  }

  getMatchNote(matchId: number): Promise<ClaudeNote> {
    return request<ClaudeNote>(this.config, `/matches/${matchId}/notes`);
  }

  predict(matchId: number, body: PredictRequest = { force_refit: false }): Promise<PredictResponse> {
    return request<PredictResponse>(this.config, `/matches/${matchId}/predict`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  /** URL for the SSE stream — callers open `EventSource(url)`. */
  notesEventsUrl(): string {
    return `${this.config.baseUrl}/events/notes`;
  }
}

/** Module-level singleton wired from Vite env. Tests build their own client. */
export const apiClient = new ApiClient({
  baseUrl: import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000",
});
