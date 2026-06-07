/**
 * React-Query hooks layered over `ApiClient` (REQ-010).
 *
 * `queryKeys` is the single source of truth for cache keys so manual
 * invalidations in mutations / SSE handlers don't drift from queries.
 */
import { useMutation, useQuery, type UseQueryOptions } from "@tanstack/react-query";

import {
  apiClient,
  type ClaudeNote,
  type Fixture,
  type MatchDetail,
  type PredictRequest,
  type PredictResponse,
} from "./client";

export const queryKeys = {
  fixtures: () => ["fixtures"] as const,
  match: (matchId: number) => ["match", matchId] as const,
  matchNote: (matchId: number) => ["match", matchId, "note"] as const,
};

type FixturesOptions = Omit<
  UseQueryOptions<Fixture[], Error, Fixture[], ReturnType<typeof queryKeys.fixtures>>,
  "queryKey" | "queryFn"
>;

export function useFixtures(options?: FixturesOptions) {
  return useQuery({
    queryKey: queryKeys.fixtures(),
    queryFn: () => apiClient.listFixtures(),
    ...options,
  });
}

type MatchOptions = Omit<
  UseQueryOptions<MatchDetail, Error, MatchDetail, ReturnType<typeof queryKeys.match>>,
  "queryKey" | "queryFn"
>;

export function useMatch(matchId: number, options?: MatchOptions) {
  return useQuery({
    queryKey: queryKeys.match(matchId),
    queryFn: () => apiClient.getMatch(matchId),
    ...options,
  });
}

type MatchNoteOptions = Omit<
  UseQueryOptions<ClaudeNote, Error, ClaudeNote, ReturnType<typeof queryKeys.matchNote>>,
  "queryKey" | "queryFn"
>;

/**
 * REQ-011 fallback: the SSE stream is the primary update channel; this hook
 * exists so the Claude-note panel can render on first paint and also serves
 * as the polling fallback in Step 8.3 when SSE drops.
 */
export function useMatchNote(matchId: number, options?: MatchNoteOptions) {
  return useQuery({
    queryKey: queryKeys.matchNote(matchId),
    queryFn: () => apiClient.getMatchNote(matchId),
    ...options,
  });
}

export function usePredictMutation(matchId: number) {
  return useMutation<PredictResponse, Error, PredictRequest | void>({
    mutationFn: (body) => apiClient.predict(matchId, body ?? undefined),
  });
}
