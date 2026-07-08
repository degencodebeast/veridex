"""Generate the frozen maker-arena fixture from the SEALED result (do NOT hand-author it).

Reads the sealed ``scripts/txline_live/cp1/maker-arena-result.json`` artifact, builds the EXACT
same envelope that ``GET /maker/arena-result`` returns — by REUSING the route's envelope builder
(:func:`veridex.api.maker_router.build_maker_arena_result_response`), never duplicating its
logic — and writes it to ``contracts/fixtures/maker_arena_result.json`` alongside the directional
fixtures (e.g. ``leaderboard.json``). The frontend binds to this frozen copy.

Run from the repo root::

    .venv/bin/python scripts/gen_maker_fixture.py
"""

from __future__ import annotations

import json
from pathlib import Path

from veridex.api.maker_router import build_maker_arena_result_response

_REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH: Path = _REPO_ROOT / "contracts" / "fixtures" / "maker_arena_result.json"


def main() -> None:
    """Build the maker envelope from the sealed result and write the frozen fixture."""
    response = build_maker_arena_result_response()
    payload = response.model_dump(mode="json")

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {FIXTURE_PATH.relative_to(_REPO_ROOT)}")


if __name__ == "__main__":
    main()
