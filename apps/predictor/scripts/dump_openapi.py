"""Dump the FastAPI app's OpenAPI schema to disk.

Used by ``make schemas`` to produce a stable, diff-able artifact at
``packages/schemas/openapi.json`` and propagate it to ``apps/ui/src/api/openapi.json``.

CI runs this and fails if the result differs from the committed file (drift catch).
"""

from __future__ import annotations

import json
from pathlib import Path

from predictor.api.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[3]
TARGETS = (
    REPO_ROOT / "packages" / "schemas" / "openapi.json",
    REPO_ROOT / "apps" / "ui" / "src" / "api" / "openapi.json",
)


def main() -> None:
    app = create_app()
    schema = app.openapi()
    payload = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    for target in TARGETS:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        print(f"wrote {target.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
