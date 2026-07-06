#!/usr/bin/env python3
"""Derive a committed fixture -> Polymarket market-identity lookup from the cp1 price-history frames.

The cp1 backfill froze `condition_id`/`token_id` into every price-history frame
(scripts/txline_live/cp1/frames/<fixture>/<side>.jsonl), but those frame files are
git-untracked -- the mapping survives only in the working tree, and the resolver that
produced it (venue/polymarket_resolver.py) is a live Gamma call with no cache that may
no longer resolve finished fixtures. This script MECHANICALLY extracts that mapping into
a small, committed, content-hashed lookup artifact so the trade-aware (MM-R1.5) lane has
a durable, verifiable key from TxLINE fixture/side -> Polymarket condition_id/token_id.

It is a PRESERVATION artifact for the 18 cp1 fixtures -- not a new result. It asserts the
ids are constant within each frame file (fail-closed on any drift) and carries the source
frame's manifest hash for provenance plus its own content hash for integrity.

Run:  python3 scripts/txline_live/build_resolved_market_lookup.py
Output: scripts/txline_live/cp1/resolved-market-lookup.json
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CP1 = HERE / "cp1"
FRAMES_DIR = CP1 / "frames"
MANIFEST_PATH = FRAMES_DIR / "backfill-manifest.json"
OUTPUT_PATH = CP1 / "resolved-market-lookup.json"
TOOL = "build_resolved_market_lookup/1"

# Fields that MUST be identical on every line of a single frame file. If any of these
# drift within a file the mapping is untrustworthy (the v1 market-key failure class),
# so we abort instead of silently picking one.
INVARIANT_FIELDS = ("fixture_id", "market_ref", "venue", "condition_id", "token_id")


def _fail(msg: str) -> "NoReturn":  # type: ignore[valid-type]
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _canonical(obj) -> bytes:
    """Deterministic bytes for content hashing -- compact, key-sorted, stable ordering."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load_manifest_index() -> dict[tuple[int, str], dict]:
    if not MANIFEST_PATH.exists():
        _fail(f"manifest not found: {MANIFEST_PATH}")
    manifest = json.loads(MANIFEST_PATH.read_text())
    index: dict[tuple[int, str], dict] = {}
    for fx in manifest.get("fixtures", []):
        fid = fx["fixture_id"]
        for side in fx.get("sides", []):
            index[(int(fid), side["side"])] = side
    return manifest, index


def _extract_frame_identity(path: Path) -> dict:
    """Read one <side>.jsonl and return its (asserted-constant) market identity."""
    identity: dict | None = None
    n_lines = 0
    with path.open() as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            n_lines += 1
            row = json.loads(raw)
            current = {k: row.get(k) for k in INVARIANT_FIELDS}
            if identity is None:
                identity = current
            elif current != identity:
                _fail(
                    f"{path}: invariant drift at line {lineno}: "
                    f"{current} != {identity} -- mapping cannot be trusted"
                )
    if identity is None or n_lines == 0:
        _fail(f"{path}: no frame rows found")
    for field in INVARIANT_FIELDS:
        if identity.get(field) in (None, ""):
            _fail(f"{path}: missing/empty '{field}'")
    identity["frame_rows"] = n_lines
    return identity


def main() -> None:
    if not FRAMES_DIR.exists():
        _fail(f"frames dir not found: {FRAMES_DIR}")
    manifest, manifest_index = _load_manifest_index()

    records: list[dict] = []
    for fixture_dir in sorted(FRAMES_DIR.iterdir()):
        if not fixture_dir.is_dir():
            continue
        for frame_file in sorted(fixture_dir.glob("*.jsonl")):
            side = frame_file.stem  # home | away | draw
            identity = _extract_frame_identity(frame_file)

            # market_ref is "1X2|<side>|full" -- cross-check the filename against the data.
            ref_parts = str(identity["market_ref"]).split("|")
            if len(ref_parts) >= 2 and ref_parts[1] != side:
                _fail(
                    f"{frame_file}: side '{side}' disagrees with market_ref "
                    f"'{identity['market_ref']}'"
                )

            fid = int(identity["fixture_id"])
            man = manifest_index.get((fid, side), {})
            rel = f"scripts/txline_live/cp1/frames/{fixture_dir.name}/{frame_file.name}"
            records.append(
                {
                    "fixture_id": fid,
                    "side": side,
                    "market_ref": identity["market_ref"],
                    "venue": identity["venue"],
                    "condition_id": identity["condition_id"],
                    "token_id": identity["token_id"],
                    "frame_rows": identity["frame_rows"],
                    "source_frames_file": man.get("frames_file", rel),
                    "source_artifact_content_hash": man.get("artifact_content_hash"),
                }
            )

    records.sort(key=lambda r: (r["fixture_id"], r["side"]))

    # Reject accidental duplicate (fixture_id, side) keys.
    seen: set[tuple[int, str]] = set()
    for r in records:
        key = (r["fixture_id"], r["side"])
        if key in seen:
            _fail(f"duplicate (fixture_id, side): {key}")
        seen.add(key)

    content_hash = hashlib.sha256(_canonical(records)).hexdigest()
    fixtures = sorted({r["fixture_id"] for r in records})

    artifact = {
        "tool": TOOL,
        "description": (
            "Mechanically-derived, content-hashed lookup from TxLINE fixture/side to "
            "Polymarket condition_id/token_id, extracted from the cp1 price-history "
            "frames. Preservation artifact for the 18 cp1 fixtures (MM-R1/R1.5 universe); "
            "NOT a new result. Regenerate with build_resolved_market_lookup.py."
        ),
        "source_manifest": {
            "tool": manifest.get("tool"),
            "coverage_artifact_hash": manifest.get("coverage_artifact_hash"),
            "fidelity_s": manifest.get("fidelity_s"),
        },
        "fixture_count": len(fixtures),
        "side_count": len(records),
        "content_hash": content_hash,
        "records": records,
    }

    OUTPUT_PATH.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUTPUT_PATH}")
    print(f"  fixtures={len(fixtures)} sides={len(records)} content_hash={content_hash}")
    missing = [f"{r['fixture_id']}/{r['side']}" for r in records if not r["source_artifact_content_hash"]]
    if missing:
        print(f"  WARNING: {len(missing)} sides lack a manifest artifact hash: {missing}")


if __name__ == "__main__":
    main()
