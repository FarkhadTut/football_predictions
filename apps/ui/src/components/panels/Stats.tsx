import type { ReactElement } from "react";

import type { MatchDetail } from "../../api/client";

export interface StatsPanelProps {
  match: MatchDetail | undefined;
  isLoading: boolean;
  error: Error | null;
}

export function StatsPanel({ match, isLoading, error }: StatsPanelProps): ReactElement {
  return (
    <section aria-labelledby="stats-panel-heading">
      <h2 id="stats-panel-heading">Stats</h2>
      {isLoading && <p>Loading match…</p>}
      {error && <p role="alert">Failed to load match: {error.message}</p>}
      {match && (
        <dl>
          <dt>Competition</dt>
          <dd>
            {match.competition} {match.season}
          </dd>
          <dt>Teams</dt>
          <dd>
            {match.home_team} vs {match.away_team}
          </dd>
          <dt>Kickoff</dt>
          <dd>{match.kickoff_utc}</dd>
          <dt>Status</dt>
          <dd>{match.status}</dd>
          {match.home_goals !== null && match.home_goals !== undefined && (
            <>
              <dt>Final score</dt>
              <dd>
                {match.home_goals} – {match.away_goals ?? 0}
              </dd>
            </>
          )}
        </dl>
      )}
    </section>
  );
}
