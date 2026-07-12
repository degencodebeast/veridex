"""E9-T3: the operator ``prepare``/``seal`` capture-and-pin CLI (composition-only).

This CLI adds **no** decode / hash / pin / seal logic of its own. It orchestrates the
already-built pieces:

* E3 capture — :func:`veridex.maker.capture.capture_order_filled_artifact`
* E1/E2 pinning — :func:`veridex.maker.trade_artifact.load_trade_artifact`,
  :func:`veridex.maker.trade_artifact.recompute_artifact_hash`,
  :func:`veridex.maker.config.build_maker_run_config`
* E9 sealed runner — :func:`veridex.maker.runner.run_maker_arena`

It is deliberately SPLIT into two subcommands so scoring can never self-pin its own
config. An atomic capture-then-seal would set ``expected == cfg.config_hash()`` — a
tautology that can never VOID. Instead:

``prepare --from-block N --to-block M --out PATH``
    Captures the ``OrderFilled`` artifact (fail-closed on a missing operator token),
    writes it to ``PATH``, then WRITES + PRINTS a pin-manifest carrying
    ``trade_artifact_hash`` and ``config_hash``. This manifest is the
    **predeclaration** the operator reviews and commits. ``prepare`` NEVER seals.

``seal --artifact PATH --expected-config-hash HASH``
    Rebuilds the config from the committed artifact and runs the sealed arena with the
    operator's PASSED ``HASH`` (the committed predeclaration). The CLI MUST NOT recompute
    the expected hash from the live cfg: :func:`run_maker_arena`'s ``verify_pinned`` VOIDs
    (raising :class:`~veridex.maker.config.MakerVoidError`) if the live config drifted from
    the predeclaration. That ``MakerVoidError`` is allowed to propagate loudly — a drifted
    seal must fail, never quietly write a result.

The live network pull is operator-only (not exercised in CI/tests): the log source is
built from the operator token in :func:`_operator_log_source`, imported lazily so this
module never requires a network SDK and tests monkeypatch the capture entirely. The
operator token (``HYPERSYNC_API``) is never printed, never written into any artifact or
manifest, and never returned.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from veridex.maker.capture import (
    OrderFilledLogSource,
    capture_order_filled_artifact,
)
from veridex.maker.config import build_maker_run_config
from veridex.maker.runner import CP1_18, RESULT_PATH, run_maker_arena
from veridex.maker.trade_artifact import load_trade_artifact, recompute_artifact_hash

__all__ = ["build_parser", "main"]


def _pin_manifest_path(out_path: Path) -> Path:
    """The committed predeclaration path that sits beside the artifact (``*.pin.json``)."""
    return out_path.with_suffix(".pin.json")


def _operator_log_source() -> OrderFilledLogSource | None:
    """Build the operator's live ``OrderFilledLogSource`` from the token, or ``None``.

    Returns ``None`` when the ``HYPERSYNC_API`` token is absent (so the capture fails
    closed) or when the operator's network adapter is not wired in this checkout (the
    live pull is an operator-only, non-CI step). The adapter is imported lazily so this
    module never depends on a network SDK and tests never hit the network. The token is
    only handed to the adapter constructor — never printed, logged, or returned.
    """
    token = os.environ.get("HYPERSYNC_API")
    if not token:
        return None
    try:
        # Operator-supplied network adapter (outside the stdlib-only trust module and
        # outside the scored path). Absent in CI/tests, so the capture fails closed.
        from scripts.maker.hypersync_source import build_hypersync_source  # type: ignore
    except ImportError:
        return None
    return build_hypersync_source(token=token)


def _scrub_token(text: str) -> str:
    """Redact any live ``HYPERSYNC_API`` token value from ``text``.

    The operator token is the one secret this CLI must never emit. Scrubbing the raw
    token value (rather than trusting an exception's type or provenance) preserves the
    no-leak guarantee even when the exception surfaces from OUTSIDE this module -- e.g.
    a live network-SDK error whose message embeds the token in a request URL. When no
    token is set (the fail-closed no-token case), this is a no-op: the existing clean
    guard message passes through unchanged.
    """
    token = os.environ.get("HYPERSYNC_API")
    if not token:
        return text
    return text.replace(token, "[REDACTED]")


def _cmd_prepare(args: argparse.Namespace) -> int:
    """Capture the artifact and WRITE + PRINT the pin-manifest predeclaration.

    Never seals. Fails closed (non-zero exit, no partial artifact) on ANY capture
    exception -- not just the ``RuntimeError`` fail-closed guard. On the operator LIVE
    path (token present + real adapter), a network-SDK exception is typically NOT a
    ``RuntimeError`` and could otherwise propagate as a raw traceback that embeds the
    operator token (e.g. in a request URL). Every exception's text is scrubbed of the
    live token value before it is ever printed.
    """
    out_path = Path(args.out)
    try:
        capture_order_filled_artifact(
            from_block=args.from_block,
            to_block=args.to_block,
            out_path=out_path,
            client=_operator_log_source(),
        )
    except Exception as exc:
        # Fail-closed on ANY exception. The no-token guard's RuntimeError is token-free
        # by construction (static strings, no network I/O yet), so scrubbing is a no-op
        # there and the existing clean message is preserved verbatim. Any other
        # exception -- including a non-RuntimeError SDK error on the LIVE path -- has its
        # text scrubbed of the live token value so it can never leak to stderr.
        print(f"capture failed closed: {_scrub_token(str(exc))}", file=sys.stderr)
        raise SystemExit(2) from exc

    # Reload the persisted artifact and derive the predeclaration purely by composition.
    art = load_trade_artifact(out_path)
    cfg = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=art)
    trade_artifact_hash = recompute_artifact_hash(list(art.rows))
    config_hash = cfg.config_hash()

    manifest = {
        "trade_artifact_hash": trade_artifact_hash,
        "config_hash": config_hash,
        "out_path": str(out_path),
        "from_block": args.from_block,
        "to_block": args.to_block,
        "rows_matched_cp1": art.rows_matched_cp1,
    }
    pin_path = _pin_manifest_path(out_path)
    pin_path.write_text(json.dumps(manifest, sort_keys=True, indent=2))

    print(f"trade_artifact_hash: {trade_artifact_hash}")
    print(f"config_hash: {config_hash}")
    print(f"pin-manifest written: {pin_path}")
    print(
        "review the pin-manifest, COMMIT it, then run:\n"
        f"  seal --artifact {out_path} --expected-config-hash {config_hash}"
    )
    return 0


def _cmd_seal(args: argparse.Namespace) -> int:
    """Run the sealed arena using the operator's PASSED predeclared config hash.

    ``--expected-config-hash`` is the committed predeclaration — the CLI never recomputes
    it from the live cfg (the anti-self-pin invariant). A live-config drift VOIDs inside
    :func:`run_maker_arena` (``MakerVoidError``), which is allowed to propagate.
    """
    artifact_path = Path(args.artifact)
    cfg = build_maker_run_config(
        fixture_ids=CP1_18,
        trade_artifact=load_trade_artifact(artifact_path),
    )
    result = run_maker_arena(
        cfg,
        expected_config_hash=args.expected_config_hash,  # PASSED predeclaration, NOT cfg.config_hash()
        trade_artifact_path=artifact_path,
        seal=True,
    )
    print(f"sealed rung: {result.rung}")
    print(f"trade_artifact_hash: {cfg.trade_artifact_hash}")
    print(f"real_executable_edge_bps: {result.real_executable_edge_bps}")
    print(f"sealed result written: {RESULT_PATH}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the ``prepare``/``seal`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="capture_and_pin",
        description="Operator capture-and-pin CLI: prepare (predeclare) then seal (score).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prep = sub.add_parser(
        "prepare",
        help="capture the OrderFilled artifact and write+print the pin-manifest predeclaration",
    )
    prep.add_argument("--from-block", type=int, required=True, dest="from_block")
    prep.add_argument("--to-block", type=int, required=True, dest="to_block")
    prep.add_argument("--out", required=True, help="destination path for the TradeArtifact JSON")
    prep.set_defaults(func=_cmd_prepare)

    seal = sub.add_parser(
        "seal",
        help="run the sealed arena against a prepared artifact using the predeclared hash",
    )
    seal.add_argument("--artifact", required=True, help="path to the prepared TradeArtifact JSON")
    seal.add_argument(
        "--expected-config-hash",
        required=True,
        dest="expected_config_hash",
        help="the operator's committed predeclared config hash (from prepare's pin-manifest)",
    )
    seal.set_defaults(func=_cmd_seal)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and dispatch to the selected subcommand."""
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
