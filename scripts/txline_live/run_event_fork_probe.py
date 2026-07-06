"""E6 pinned, self-verifying event-window fork-probe runner (PAT-001 / CON-012).

The executable pre-run stamp for the lag-vs-overreaction fork probe: the pinned
:class:`ProbeConfig` identity (:data:`EXPECTED_CONFIG_HASH`) and the predeclared
fixture universe (:data:`PINNED_FIXTURES`) are fixed in this committed file BEFORE
any verdict exists, mirroring ``run002_vvv``. The single self-verify VOIDs the run
on ANY config drift -- :func:`verify_pinned` recomputes the live config hash and
raises :class:`ProbeVoidError` BEFORE the runner touches a single pack or scores
file (the Run-002 fail-closed-before-I/O precedent, AC-007).

Trust boundary (CON-012, rung-1): this runner and the whole ``event_probe`` package
import NOTHING from the trust core (``veridex.law`` / ``veridex.scoring`` /
``veridex.verifier`` / ``veridex.checks`` / ``veridex.runtime.evidence``). It reads
goals from the NON-EVIDENCE ``scores_<fid>.json`` sibling of each ReplayPack and
replays market states through the same normalizer live TxLINE uses
(``veridex.ingest`` is a permitted dependency); it never mutates a pack and never
touches a scored/law/policy/proof surface (AC-001).

Writing the sealed artifact is the OPERATOR-GATED ``seal=True`` step. The default
``run_probe(seal=False)`` returns the sealed dict and writes NOTHING, so the test
suite exercises the whole pipeline without ever producing a result file. The
operator run (``main`` / ``seal=True``) is gated by a Codex milestone review; DO
NOT run it in CI.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from veridex.backtest.event_probe.aggregate import aggregate_verdict  # noqa: E402
from veridex.backtest.event_probe.compute import compute_event_record  # noqa: E402
from veridex.backtest.event_probe.config import (  # noqa: E402
    ProbeConfig,
    build_sealed_result,
    verify_pinned,
)
from veridex.backtest.event_probe.extraction import extract_goal_events  # noqa: E402
from veridex.backtest.event_probe.series import build_tracked_series  # noqa: E402
from veridex.backtest.event_probe.slices import derive_slice_tags  # noqa: E402
from veridex.ingest.replay_pack import load_pack_marketstates  # noqa: E402

#: The pinned ProbeConfig identity (``ProbeConfig().config_hash()``), hard-coded so a
#: post-hoc threshold change diverges the live hash and VOIDs the run (CON-014).
#: Re-pinned for v2 when the series-selection surface (market_1x2_key /
#: in_running_phase) was sealed into ProbeConfig -- the full-match ``||`` 1X2 market
#: replaces the first-half ``|half=1|`` one. (v1 hash was
#: 10d6986f7fe57d90f5256dd998ae3fc3598b15853a73f6dec37b32762048e259.)
#: Recompute: ``.venv/bin/python -c "from veridex.backtest.event_probe.config import
#: ProbeConfig; print(ProbeConfig().config_hash())"``.
EXPECTED_CONFIG_HASH = "2be65639490535092934713de1aefba237982c45141a3b5d8effd4f8115d2e76"

#: The predeclared fixture universe (v2 objective re-stamp). ALL finished competition-72
#: (FIFA WC 2026) fixtures discoverable via the TxLINE backfill path as of
#: 2026-07-06T00:50:44Z with both odds+scores legs (verify=True) and the full-match
#: ``1X2_PARTICIPANT_RESULT||`` key present: **67 included, data-exhausted** (bounded by
#: the TxLINE scores-feed retention horizon, NOT by discretion). 26 fixtures excluded with
#: named reasons + 6 not-yet-finished; the full frozen manifest (IDs, pack content-hashes,
#: named exclusions, discovery query) is at
#: ``.omc/research/event-fork-probe-universe-manifest.md`` (Codex-blessed). Allowed
#: conclusions: FOLLOW / FADE / SPLIT-BY-SLICE / INCONCLUSIVE-no-build. Frozen BEFORE any
#: full-67 verdict was computed (out-of-sample for the 49 packs added post-original-18).
#: Supersedes the original 18-fixture run002_vvv roster.
PINNED_FIXTURES: tuple[int, ...] = (
    17588223, 17588229, 17588231, 17588232, 17588234, 17588235, 17588236, 17588238,
    17588240, 17588242, 17588244, 17588245, 17588302, 17588303, 17588309, 17588310,
    17588313, 17588314, 17588317, 17588319, 17588320, 17588321, 17588323, 17588324,
    17588325, 17588326, 17588388, 17588389, 17588390, 17588391, 17588395, 17588397,
    17588398, 17588401, 17588402, 17588404, 17926593, 17926603, 17926615, 17926647,
    17926686, 17926687, 17926688, 17926704, 17926740, 17926764, 17926765, 17926766,
    18167317, 18172280, 18172379, 18172469, 18175397, 18175918, 18175981, 18175983,
    18176123, 18179549, 18179550, 18179551, 18179552, 18179759, 18179763, 18179764,
    18185036, 18187298, 18188721,
)

#: The ReplayPack universe root (each fixture is a ``packs/<fid>/`` directory).
PACKS_DIR = Path(__file__).parent / "packs"

#: The sealed-artifact path -- written ONLY by the operator ``seal=True`` step, never
#: by ``run_probe(seal=False)`` and never by the test suite (mirrors ``run002_vvv``).
RESULT_PATH = ROOT.parent / ".omc" / "research" / "edge-validation-runs" / "event-fork-probe-result.json"


def run_probe(cfg: ProbeConfig | None = None, *, seal: bool = False) -> dict[str, Any]:
    """Run the pinned fork probe over :data:`PINNED_FIXTURES`; return the sealed dict.

    VOIDs (:class:`ProbeVoidError`) BEFORE any I/O when ``cfg`` drifts from
    :data:`EXPECTED_CONFIG_HASH` (PAT-001 / AC-007). Then, per fixture, replays the
    pack's market states and reads goals from the non-evidence ``scores_<fid>.json``
    sibling; each goal event is windowed against the scoring participant's tracked
    1X2 series and classified, carrying the home/away label into ``slice_tags``.
    The per-event records are aggregated into the predeclared verdict and serialized
    via :func:`build_sealed_result`.

    Args:
        cfg: The probe config; defaults to the pinned :class:`ProbeConfig`.
        seal: When ``True`` (OPERATOR-GATED), write the sealed dict to
            :data:`RESULT_PATH`. Default ``False`` returns the dict and writes
            NOTHING -- the path every test uses.

    Returns:
        The sealed result artifact dict (§4): pinned config + hash, overall verdict
        with global stats and per-slice verdicts, the full per-event
        ``event_records[]`` audit trail, and both tally maps.

    Raises:
        ProbeVoidError: If the live config hash diverges from the pinned stamp.
    """
    cfg = cfg or ProbeConfig()
    # VOID-on-drift precedes ALL reads: fail closed before touching any data.
    verify_pinned(cfg, EXPECTED_CONFIG_HASH)

    window_cfg = cfg.to_window_config()
    records = []
    total_goal_events = 0
    extraction_excluded: dict[str, int] = {}
    for fixture_id in PINNED_FIXTURES:
        pack_dir = PACKS_DIR / str(fixture_id)
        states = load_pack_marketstates(pack_dir, fixture_id, verify=True)
        scores = json.loads((pack_dir / f"scores_{fixture_id}.json").read_text())
        extraction = extract_goal_events(scores)
        total_goal_events += len(extraction.events)
        # Thread each fixture's extraction rejects through so §4's excluded_by_reason
        # spans extraction (decreasing_score / ambiguous_delta / unparseable) as well
        # as the compute reasons -- no reject is dropped before aggregation.
        for reason, count in extraction.excluded.items():
            extraction_excluded[reason] = extraction_excluded.get(reason, 0) + count
        for event in extraction.events:
            series = build_tracked_series(states, event.participant, cfg)
            record = compute_event_record(series, event, window_cfg)
            # Derive ALL FIVE CON-007 slice dimensions (the tracked series is
            # participant-keyed and knows no home/away, favorite status, score
            # context, or timing -- those come from the event + its window record).
            slice_tags = derive_slice_tags(event, record, cfg)
            record = replace(record, slice_tags=slice_tags)
            records.append(record)

    result = aggregate_verdict(records, cfg.to_agg_config())
    sealed = build_sealed_result(
        cfg,
        result,
        records,
        fixtures=list(PINNED_FIXTURES),
        total_goal_events=total_goal_events,
        extraction_excluded=extraction_excluded,
    )

    if seal:
        RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULT_PATH.write_text(json.dumps(sealed, indent=1, default=str))
    return sealed


def main() -> None:
    """OPERATOR-GATED entrypoint: run the probe and SEAL the artifact to disk.

    Reads local packs + score siblings only (no network, no creds). Gated by a
    Codex milestone review; DO NOT run in CI.
    """
    sealed = run_probe(seal=True)
    print(f"verdict={sealed['verdict']} "
          f"global_n={sealed['global']['n']} "
          f"config_hash={sealed['config_hash'][:12]}")


if __name__ == "__main__":
    main()
