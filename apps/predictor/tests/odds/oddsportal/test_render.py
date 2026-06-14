"""RenderedCache offline/cache-hit behavior (never launches a browser here)."""

from __future__ import annotations

from pathlib import Path

import pytest

from predictor.odds.oddsportal.render import RenderedCache


def test_cache_hit_returns_without_browser(tmp_path: Path) -> None:
    (tmp_path / "page_p1.html").write_text("<html>cached</html>", encoding="utf-8")
    cache = RenderedCache(tmp_path, offline=True)
    assert cache.get("https://example.test/p1", "page_p1") == "<html>cached</html>"


def test_offline_miss_raises(tmp_path: Path) -> None:
    cache = RenderedCache(tmp_path, offline=True)
    with pytest.raises(FileNotFoundError, match="offline cache miss"):
        cache.get("https://example.test/missing", "missing_key")
