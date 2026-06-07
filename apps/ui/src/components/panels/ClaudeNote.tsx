import type { ReactElement } from "react";

import type { ClaudeNote as ClaudeNoteData } from "../../api/client";

export interface ClaudeNotePanelProps {
  note: ClaudeNoteData | undefined;
  isLoading: boolean;
  error: Error | null;
  /** When `true`, render a small "live" indicator; otherwise show "polling". */
  streamConnected: boolean;
}

const PLACEHOLDER = "Awaiting Claude analysis…";

function formatShift(shift: number): string {
  const sign = shift > 0 ? "+" : "";
  return `${sign}${shift.toFixed(2)}`;
}

export function ClaudeNotePanel({
  note,
  isLoading,
  error,
  streamConnected,
}: ClaudeNotePanelProps): ReactElement {
  return (
    <section aria-labelledby="claude-panel-heading">
      <h2 id="claude-panel-heading">Claude note</h2>
      <p aria-label="stream status">
        Stream: <strong>{streamConnected ? "live" : "polling"}</strong>
      </p>
      {isLoading && <p>Loading note…</p>}
      {error && <p role="alert">Failed to load note: {error.message}</p>}
      {!isLoading && !error && !note && <p>{PLACEHOLDER}</p>}
      {note && (
        <article>
          <p>{note.summary}</p>
          <p>
            Confidence: <strong>{(note.confidence * 100).toFixed(0)}%</strong>
          </p>
          {note.qualitative_deltas.length > 0 && (
            <>
              <h3>Qualitative shifts</h3>
              <ul>
                {note.qualitative_deltas.map((d) => (
                  <li key={d.market}>
                    {d.market}: {formatShift(d.log_odds_shift)} log-odds
                  </li>
                ))}
              </ul>
            </>
          )}
          {note.sources.length > 0 && (
            <>
              <h3>Sources</h3>
              <ul>
                {note.sources.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </>
          )}
        </article>
      )}
    </section>
  );
}
