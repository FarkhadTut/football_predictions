/**
 * Match detail page (REQ-010, REQ-011, Sub-step 8.3).
 *
 * Three-panel layout mandated by the brainstorm sketch:
 *   ┌──────────┬──────────┬──────────────┐
 *   │  Stats   │  Model   │  Claude note │
 *   └──────────┴──────────┴──────────────┘
 *
 * The Claude-note panel is the live one — `useNotesStream` writes fresh
 * notes straight into the react-query cache. When the SSE connection
 * drops we flip `useMatchNote` to a 10s polling refetch so the UI keeps
 * up while reconnect attempts run in the background.
 */
import type { ReactElement } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError } from "../api/client";
import { ClaudeNotePanel } from "../components/panels/ClaudeNote";
import { ModelPanel } from "../components/panels/Model";
import { StatsPanel } from "../components/panels/Stats";
import { useMatch, useMatchNote } from "../api/queries";
import { useNotesStream } from "../hooks/useNotesStream";

const POLL_FALLBACK_MS = 10_000;

function isMissingNoteError(error: Error | null | undefined): boolean {
  return error instanceof ApiError && error.status === 404;
}

export function Match(): ReactElement {
  const { matchId: matchIdParam } = useParams<{ matchId: string }>();
  const matchId = Number(matchIdParam);

  if (!Number.isFinite(matchId) || matchId <= 0) {
    return (
      <section>
        <h1>Match</h1>
        <p role="alert">Invalid match id.</p>
        <Link to="/">Back to fixtures</Link>
      </section>
    );
  }

  return <MatchView matchId={matchId} />;
}

interface MatchViewProps {
  matchId: number;
}

function MatchView({ matchId }: MatchViewProps): ReactElement {
  const matchQuery = useMatch(matchId);
  const { connected } = useNotesStream(matchId);
  const noteQuery = useMatchNote(matchId, {
    refetchInterval: connected ? false : POLL_FALLBACK_MS,
  });

  return (
    <main>
      <p>
        <Link to="/">← Fixtures</Link>
      </p>
      <h1>Match #{matchId}</h1>
      <div role="group" aria-label="match panels">
        <StatsPanel
          match={matchQuery.data}
          isLoading={matchQuery.isLoading}
          error={matchQuery.error ?? null}
        />
        <ModelPanel matchId={matchId} />
        <ClaudeNotePanel
          note={noteQuery.data}
          isLoading={noteQuery.isLoading}
          // 404 means "no note yet" — surface that as the placeholder, not as an error.
          error={isMissingNoteError(noteQuery.error) ? null : (noteQuery.error ?? null)}
          streamConnected={connected}
        />
      </div>
    </main>
  );
}
