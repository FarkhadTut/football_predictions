/**
 * Fixtures list page (REQ-010, Sub-step 8.2).
 *
 * Groups upcoming fixtures by kickoff date (UTC) so users scanning the
 * tournament can see a day-by-day view. Each row navigates to the match
 * page wired in Sub-step 8.3.
 */
import type { ReactElement } from "react";
import { Link } from "react-router-dom";

import type { Fixture } from "../api/client";
import { useFixtures } from "../api/queries";

/** "2026-06-11T12:00:00Z" → "2026-06-11" (UTC). */
function dateKey(kickoffUtc: string): string {
  return kickoffUtc.slice(0, 10);
}

/** "2026-06-11T12:00:00Z" → "12:00 UTC". */
function timeLabel(kickoffUtc: string): string {
  return `${kickoffUtc.slice(11, 16)} UTC`;
}

interface DayGroup {
  date: string;
  fixtures: Fixture[];
}

function groupByDate(fixtures: Fixture[]): DayGroup[] {
  const sorted = [...fixtures].sort((a, b) => a.kickoff_utc.localeCompare(b.kickoff_utc));
  const groups = new Map<string, Fixture[]>();
  for (const f of sorted) {
    const key = dateKey(f.kickoff_utc);
    const bucket = groups.get(key);
    if (bucket) {
      bucket.push(f);
    } else {
      groups.set(key, [f]);
    }
  }
  return Array.from(groups, ([date, list]) => ({ date, fixtures: list }));
}

export function Fixtures(): ReactElement {
  const query = useFixtures();

  if (query.isLoading) {
    return (
      <section aria-busy="true">
        <h1>Fixtures</h1>
        <p>Loading fixtures…</p>
      </section>
    );
  }

  if (query.isError) {
    return (
      <section>
        <h1>Fixtures</h1>
        <p role="alert">Failed to load fixtures: {query.error.message}</p>
      </section>
    );
  }

  const fixtures = query.data ?? [];
  if (fixtures.length === 0) {
    return (
      <section>
        <h1>Fixtures</h1>
        <p>No upcoming fixtures.</p>
      </section>
    );
  }

  const groups = groupByDate(fixtures);

  return (
    <section>
      <h1>Fixtures</h1>
      {groups.map(({ date, fixtures: dayFixtures }) => (
        <article key={date} aria-labelledby={`day-${date}`}>
          <h2 id={`day-${date}`}>{date}</h2>
          <ul>
            {dayFixtures.map((fixture) => (
              <li key={fixture.id}>
                <Link to={`/matches/${fixture.id}`}>
                  <span>{timeLabel(fixture.kickoff_utc)}</span>
                  <span>
                    {" — "}
                    {fixture.home_team} vs {fixture.away_team}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </article>
      ))}
    </section>
  );
}
