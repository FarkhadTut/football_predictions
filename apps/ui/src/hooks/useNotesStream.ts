/**
 * SSE consumer for `/events/notes` (REQ-011, Sub-step 8.3).
 *
 * Opens one `EventSource` per mounted match page, listens for
 * `note.updated` events, and — when the `match_id` matches — writes the
 * parsed note straight into the react-query cache. This keeps the
 * `useMatchNote(matchId)` hook reactive without a manual refetch.
 *
 * If the connection errors out, we expose `connected: false` so the
 * consumer can enable polling fallback (`refetchInterval`). `EventSource`
 * auto-reconnects on its own; we flip back to `connected: true` once a
 * fresh `open` fires.
 *
 * `note.invalid` events are surfaced via `onInvalid` for the (future)
 * toast/notification surface; the panel itself keeps the last good note.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { apiClient, type ClaudeNote } from "../api/client";
import { queryKeys } from "../api/queries";

export interface NoteInvalidEvent {
  match_id: number;
  errors: unknown[];
}

export interface UseNotesStreamOptions {
  /** Inject a constructor in tests; defaults to the browser `EventSource`. */
  EventSourceCtor?: typeof EventSource;
  /** Called when a `note.invalid` event arrives for the watched match. */
  onInvalid?: (event: NoteInvalidEvent) => void;
}

export interface UseNotesStreamResult {
  /** `true` while the EventSource has an open connection. */
  connected: boolean;
}

export function useNotesStream(
  matchId: number,
  options: UseNotesStreamOptions = {},
): UseNotesStreamResult {
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);

  // Stash mutable callbacks in a ref so changing them across renders
  // doesn't tear down the EventSource.
  const onInvalidRef = useRef(options.onInvalid);
  onInvalidRef.current = options.onInvalid;

  const Ctor = options.EventSourceCtor ?? (typeof EventSource !== "undefined" ? EventSource : null);

  useEffect(() => {
    if (Ctor === null) {
      return;
    }
    const source = new Ctor(apiClient.notesEventsUrl());

    const handleOpen = () => setConnected(true);
    const handleError = () => setConnected(false);

    const handleUpdated = (event: MessageEvent) => {
      let note: ClaudeNote;
      try {
        note = JSON.parse(event.data) as ClaudeNote;
      } catch {
        return;
      }
      if (note.match_id !== matchId) {
        return;
      }
      queryClient.setQueryData(queryKeys.matchNote(matchId), note);
    };

    const handleInvalid = (event: MessageEvent) => {
      let payload: NoteInvalidEvent;
      try {
        payload = JSON.parse(event.data) as NoteInvalidEvent;
      } catch {
        return;
      }
      if (payload.match_id !== matchId) {
        return;
      }
      onInvalidRef.current?.(payload);
    };

    source.addEventListener("open", handleOpen);
    source.addEventListener("error", handleError);
    source.addEventListener("note.updated", handleUpdated as EventListener);
    source.addEventListener("note.invalid", handleInvalid as EventListener);

    return () => {
      source.removeEventListener("open", handleOpen);
      source.removeEventListener("error", handleError);
      source.removeEventListener("note.updated", handleUpdated as EventListener);
      source.removeEventListener("note.invalid", handleInvalid as EventListener);
      source.close();
      setConnected(false);
    };
  }, [Ctor, matchId, queryClient]);

  return { connected };
}
