"""Rendered-HTML cache for OddsPortal pages.

OddsPortal decrypts its odds in-browser, so we render each page once with a
headless undetected-Chrome (SeleniumBase), cache the resulting DOM to disk, and
serve the cache on every later run. The browser is created lazily, so cache hits
and ``offline`` mode never launch Chrome.

Mirrors the spirit of ``fbref_source.enable_offline_cache``: once a page is
cached, the backtest pipeline reads it with zero network and zero browser.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from types import TracebackType
from typing import Any

__all__ = ["RenderedCache"]

logger = logging.getLogger(__name__)


class RenderedCache:
    """Disk cache of rendered OddsPortal HTML keyed by a caller-supplied stem.

    Parameters
    ----------
    cache_dir:
        Directory holding ``<key>.html`` files. Created on first write.
    offline:
        When ``True``, a cache miss raises ``FileNotFoundError`` instead of
        rendering — guarantees no network/browser (used by tests + re-runs).
    settle_seconds:
        How long to wait after navigation for the SPA to render odds into the
        DOM before snapshotting the page source.
    """

    def __init__(
        self,
        cache_dir: Path,
        *,
        offline: bool = False,
        settle_seconds: float = 7.0,
    ) -> None:
        self._dir = Path(cache_dir)
        self._offline = offline
        self._settle = settle_seconds
        self._driver: Any | None = None

    def get(self, url: str, key: str) -> str:
        """Return rendered HTML for ``url``, from cache or a fresh render."""
        path = self._dir / f"{key}.html"
        if path.exists():
            logger.debug("oddsportal cache hit: %s", key)
            return path.read_text(encoding="utf-8")
        if self._offline:
            raise FileNotFoundError(f"oddsportal offline cache miss: {key} ({url})")
        html = self._render(url)
        self._dir.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        logger.info("oddsportal rendered + cached: %s (%d bytes)", key, len(html))
        return html

    def _render(self, url: str) -> str:
        driver = self._ensure_driver()
        driver.get(url)
        time.sleep(self._settle)
        source: str = driver.get_page_source()
        return source

    def _ensure_driver(self) -> Any:
        if self._driver is None:
            from seleniumbase import Driver  # type: ignore[import-untyped]

            self._driver = Driver(uc=True, headless=True)
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.quit()
            self._driver = None

    def __enter__(self) -> RenderedCache:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
