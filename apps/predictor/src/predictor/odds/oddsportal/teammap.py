"""Normalize OddsPortal team names to the canonical names already in ``teams``.

The ``teams`` table was populated by the FBref loader, which uses FBref's name
forms (``IR Iran``, ``Korea Republic``, ``Türkiye``, ``Côte d'Ivoire`` …).
OddsPortal uses more colloquial forms (``Iran``, ``South Korea``, ``Turkey`` …).
This map bridges the two so the loader can resolve a ``match_id``.

Only divergent names need an entry; anything not in ``ALIASES`` is returned
unchanged (most names already match: Brazil, France, Germany, Spain, …).
"""

from __future__ import annotations

__all__ = ["ALIASES", "normalize_team"]

# OddsPortal name -> FBref/DB canonical name.
ALIASES: dict[str, str] = {
    "USA": "United States",
    "South Korea": "Korea Republic",
    "Korea Republic": "Korea Republic",
    "Iran": "IR Iran",
    "Turkey": "Türkiye",
    "Türkiye": "Türkiye",
    "North Macedonia": "N. Macedonia",
    "Ivory Coast": "Côte d'Ivoire",
    "Bosnia and Herzegovina": "Bosnia-Herzegovina",
    "Bosnia & Herzegovina": "Bosnia-Herzegovina",
    "Republic of Ireland": "Rep. of Ireland",
    "Ireland": "Rep. of Ireland",
    "Czech Republic": "Czechia",
}


def normalize_team(name: str) -> str:
    """Map an OddsPortal team name to the DB canonical form.

    Trims surrounding whitespace, applies the alias table, and otherwise
    returns the name unchanged.
    """
    trimmed = name.strip()
    return ALIASES.get(trimmed, trimmed)
