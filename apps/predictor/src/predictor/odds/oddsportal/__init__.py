"""OddsPortal historical-odds ingestion (browser-render + offline cache).

OddsPortal AES-encrypts its AJAX odds payloads, so plain HTTP can't read them.
Instead a headless browser renders each page (the app decrypts in-JS), the
rendered DOM is cached to disk, and pure parser functions extract odds from the
cached HTML. See ``tasks/plans/steady-swinging-pine.md``.
"""

from predictor.odds.oddsportal.contracts import OddsRow, ParsedMatch

__all__ = ["OddsRow", "ParsedMatch"]
