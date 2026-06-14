"""Pure DOM -> data parsers for OddsPortal rendered HTML.

No I/O and no network: every function takes an HTML string (from the rendered
DOM cache) and returns plain data, so they are unit-testable against saved
fixtures. Selectors are pinned to OddsPortal's ``data-testid`` attributes, which
are more stable than CSS classes.

DOM shape (verified against a rendered WC-2018 results page):

* Each match is a ``[data-testid="game-row"]`` node. OddsPortal renders **two**
  copies per match (responsive desktop/mobile), so rows are de-duplicated by
  ``(date, home, away)``.
* Team names: ``[data-testid="event-participants"] a[title]`` — first is home,
  second is away.
* 1X2 odds: ``[data-testid^="odd-"]`` text, emitted in responsive pairs
  ``[h, h, d, d, a, a]`` — we take every other cell to get ``[home, draw, away]``.
* The match date comes from the nearest preceding ``[data-testid="date-header"]``
  in document order.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

from bs4 import BeautifulSoup, Tag

from predictor.odds.oddsportal.contracts import ParsedMatch

__all__ = ["parse_results_list"]

logger = logging.getLogger(__name__)

# "15 Jul 2018  - Play Offs" -> capture the leading "15 Jul 2018".
_DATE_RE = re.compile(r"(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})")


def _attr_str(tag: Tag, name: str) -> str:
    """Return a single string attribute value, coercing bs4's union type."""
    val = tag.get(name)
    if isinstance(val, list):
        val = val[0] if val else ""
    return (val or "").strip()


def _parse_header_date(text: str) -> date | None:
    m = _DATE_RE.search(text)
    if not m:
        # "Today" / "Yesterday" headers carry no absolute date; skip — historical
        # results pages always render absolute dates.
        return None
    try:
        return datetime.strptime(m.group(1), "%d %b %Y").date()
    except ValueError:
        return None


def _odds_triple(row: Tag) -> tuple[float, float, float] | None:
    """Extract ``(home, draw, away)`` decimal odds from a game-row.

    Odds cells are emitted in responsive pairs ``[h, h, d, d, a, a]``; taking
    every other cell yields the three distinct prices regardless of whether any
    two happen to be numerically equal.
    """
    cells = row.select('[data-testid^="odd-"]')
    texts = [c.get_text(strip=True) for c in cells]
    picked = texts[::2] if len(texts) >= 6 else texts
    if len(picked) < 3:
        return None
    try:
        vals = tuple(float(t) for t in picked[:3])
    except ValueError:
        return None
    if not all(v > 1.0 for v in vals):
        return None
    return vals  # type: ignore[return-value]


def _participants(row: Tag) -> tuple[str, str] | None:
    anchors = row.select('[data-testid="event-participants"] a[title]')
    titles = [_attr_str(a, "title") for a in anchors]
    titles = [t for t in titles if t]
    if len(titles) < 2:
        return None
    return titles[0], titles[1]


def _score(row: Tag, home: str, away: str) -> tuple[int | None, int | None]:
    """Best-effort final score; only a cross-check, never required.

    Scoped to the participants subtree (which holds the scores, not the odds
    cells). Each score is rendered twice (responsive), i.e. the digit tokens run
    ``[home, home, away, away]`` — take every other so equal scores survive.
    """
    block = row.select_one('[data-testid="event-participants"]')
    if block is None:
        return None, None
    text = block.get_text(" ", strip=True).replace(home, " ").replace(away, " ")
    digits = re.findall(r"\d{1,2}", text)
    picked = digits[::2] if len(digits) >= 4 else digits
    if len(picked) < 2:
        return None, None
    return int(picked[0]), int(picked[1])


def _detail_path(row: Tag) -> str | None:
    a = row.select_one('a[href*="/football/"]')
    if a is None:
        return None
    href = _attr_str(a, "href")
    if not href:
        return None
    return href.split("#", 1)[0]


def parse_results_list(html: str) -> list[ParsedMatch]:
    """Parse one OddsPortal tournament results page into matches with 1X2 odds.

    Rows lacking two participants or a valid odds triple are skipped (logged at
    debug). Responsive duplicate rows are collapsed by ``(date, home, away)``.
    """
    soup = BeautifulSoup(html, "lxml")
    nodes = soup.select('[data-testid="date-header"],[data-testid="game-row"]')

    out: dict[tuple[date, str, str], ParsedMatch] = {}
    current: date | None = None
    for node in nodes:
        testid = node.get("data-testid")
        if testid == "date-header":
            current = _parse_header_date(node.get_text(" ", strip=True))
            continue
        if current is None:
            continue
        teams = _participants(node)
        odds = _odds_triple(node)
        if teams is None or odds is None:
            continue
        home, away = teams
        key = (current, home, away)
        if key in out:
            continue  # responsive duplicate
        hs, as_ = _score(node, home, away)
        out[key] = ParsedMatch(
            match_date=current,
            home_team=home,
            away_team=away,
            home_odds=odds[0],
            draw_odds=odds[1],
            away_odds=odds[2],
            detail_path=_detail_path(node),
            home_score=hs,
            away_score=as_,
        )
    return list(out.values())
