"""T4 — pre-refactor `RunResult` golden regression pin (REQ-2D-102, AC-2D-101, spec §6).

Pins the CURRENT `run_competition` byte-output against the committed golden fixtures (happy
path + the concurrent-error/timeout path) so the T5 `feed()/finalize()` refactor can prove it
changed exactly zero sealed bytes. The fixtures are generator output only — see
``tests/golden/generate_golden.py``.
"""

import json
import pathlib

import pytest

from tests.golden.generate_golden import run_case


@pytest.mark.parametrize("case", ["happy", "error"])
def test_run_competition_matches_golden(case: str) -> None:
    golden = json.loads((pathlib.Path("tests/golden") / f"run_baseline_{case}.json").read_text())
    assert run_case(case) == golden
