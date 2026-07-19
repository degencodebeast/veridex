"""R-2 — the trusted, hash-verified ReplayPack CATALOG + writable-capture-root promotion (TDD).

The four load-bearing REDs (each fails before the R-2 catalog exists):
  (a) startup hash-verifies every pack; a TAMPERED/unverified pack in the root is EXCLUDED (fail-closed,
      never catalogued, never served);
  (b) the catalog lists BOTH real and synthetic packs with HONEST provenance + fixtures (synthetic
      labelled synthetic, a pack that merely declares genuine without a coherent state is NOT genuine);
  (c) a NEW pack dropped into the SEPARATE writable capture root hash-verifies + ATOMICALLY registers at
      RUNTIME (no restart), and an UNVERIFIED new pack is REJECTED (verify-before-promote);
  (d) the READ-ONLY curated root is NEVER written (by the startup scan OR by registration).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from veridex.ingest.capture_chain import (
    GENUINE_TXLINE_PROVENANCE,
    SYNTHETIC_PROVENANCE,
    is_genuine_pack,
    synthetic_authority,
)
from veridex.ingest.recorder import SessionMeta, envelope_line
from veridex.ingest.replay_catalog import (
    CatalogAdmissionError,
    CatalogVerificationError,
    ReplayCatalog,
    build_catalog,
)
from veridex.ingest.replay_pack import _compute_content_hash, pack_from_session, verify_content_hash

_REAL_PACK_SRC = Path(__file__).resolve().parents[1] / "scripts" / "fixtures" / "demo_pack_real"


def _odds_record(fixture_id: int, ts_ms: int) -> dict:
    return {
        "FixtureId": fixture_id,
        "Ts": ts_ms,
        "InRunning": False,
        "SuperOddsType": "1X2",
        "MarketPeriod": None,
        "MarketParameters": None,
        "PriceNames": ["Home", "Draw", "Away"],
        "Prices": [2500, 3200, 2800],
        "Pct": [35.5, 28.0, 36.5],
    }


def _make_synthetic_pack(tmp_path: Path, name: str, fixture_id: int = 5) -> Path:
    """Build a real, hash-valid SYNTHETIC pack (provenance=synthetic-illustrative) under ``name``."""
    session_dir = tmp_path / f"_session_{name}"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "records.jsonl").write_text(
        envelope_line(_odds_record(fixture_id, 100_000), 100)
        + "\n"
        + envelope_line(_odds_record(fixture_id, 131_000), 131)
        + "\n"
    )
    (session_dir / "meta.json").write_text(
        SessionMeta(started_ts=99, endpoints=["/odds/stream"], tool_version="t").model_dump_json()
    )
    out_dir = tmp_path / name
    pack_from_session(session_dir, out_dir, authority=synthetic_authority())
    return out_dir


def _copy_real_pack(dest: Path) -> Path:
    """Copy the shipped genuine curated pack (demo_pack_real) into ``dest`` and return the dir."""
    shutil.copytree(_REAL_PACK_SRC, dest)
    return dest


def _tamper_data_file(pack_dir: Path) -> None:
    """Flip a byte in a manifest-referenced data file so content_hash no longer verifies."""
    manifest = json.loads((pack_dir / "pack.json").read_text())
    data_file = pack_dir / manifest["fixtures"][0]["records"]
    raw = bytearray(data_file.read_bytes())
    raw[0] ^= 0xFF
    data_file.write_bytes(bytes(raw))


def _dir_fingerprint(root: Path) -> dict[str, tuple[int, bytes]]:
    """Map each file under ``root`` -> (mtime_ns, raw file bytes) to prove the tree is untouched."""
    out: dict[str, tuple[int, bytes]] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_mtime_ns, p.read_bytes())
    return out


# ---------------------------------------------------------------------------
# RED (a) — startup hash-verifies every pack; a TAMPERED pack is EXCLUDED (fail-closed)
# ---------------------------------------------------------------------------


def test_red_a_tampered_pack_excluded_fail_closed(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    curated.mkdir()
    _make_synthetic_pack(tmp_path, "curated/good", fixture_id=5)
    tampered = _make_synthetic_pack(tmp_path, "curated/bad", fixture_id=7)
    _tamper_data_file(tampered)

    catalog = build_catalog(curated)

    assert "good" in catalog  # hash-verified pack IS catalogued
    assert "bad" not in catalog  # tampered pack EXCLUDED — never catalogued, never served
    assert catalog.pack_ids() == ["good"]
    # And the excluded pack really is tamper-detectable (guards against a false-positive test).
    assert verify_content_hash(tampered) is False


def test_red_a_malformed_manifest_excluded(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    curated.mkdir()
    _make_synthetic_pack(tmp_path, "curated/good")
    broken = curated / "broken"
    broken.mkdir()
    (broken / "pack.json").write_text("{ not json")

    catalog = build_catalog(curated)

    assert catalog.pack_ids() == ["good"]
    assert "broken" not in catalog


def _reseal_manifest_fixture_id(pack_dir: Path, new_fixture_id: object) -> None:
    """Set fixtures[0].fixture_id to ``new_fixture_id`` and RE-SEAL content_hash so verify still PASSES.

    The record file list (``records`` filenames) is unchanged, so the recomputed hash matches — this
    isolates the honesty gate (a bool/non-int fixture id), NOT tamper-evidence, as the reason for exclusion.
    """
    manifest = json.loads((pack_dir / "pack.json").read_text())
    manifest["fixtures"][0]["fixture_id"] = new_fixture_id
    manifest["content_hash"] = _compute_content_hash(
        pack_dir, manifest["fixtures"], pack_version=int(manifest["pack_version"]), capture=manifest["capture"]
    )
    (pack_dir / "pack.json").write_text(json.dumps(manifest))


@pytest.mark.parametrize("bad_fixture_id", [True, False, "5", 5.0, None])
def test_red_bool_or_nonint_fixture_id_excludes_pack_fail_closed(
    tmp_path: Path, bad_fixture_id: object
) -> None:
    """A JSON bool (or any non-int) fixture id must NOT be admitted as a fixture id (bool subclasses int),
    and a malformed fixture entry EXCLUDES the whole pack (fail-closed) rather than silently under-reporting
    its fixtures. A hash-VALID sibling pack is still catalogued, proving the exclusion is entry-specific."""
    curated = tmp_path / "curated"
    curated.mkdir()
    _make_synthetic_pack(tmp_path, "curated/good", fixture_id=5)
    bad = _make_synthetic_pack(tmp_path, "curated/bad", fixture_id=7)
    _reseal_manifest_fixture_id(bad, bad_fixture_id)
    assert verify_content_hash(bad) is True  # hash verifies — the reject is the fixture-id honesty gate

    catalog = build_catalog(curated)

    assert "bad" not in catalog  # malformed fixture entry -> whole pack excluded (fail-closed)
    assert catalog.pack_ids() == ["good"]  # the valid sibling is unaffected
    # And a bool never sneaks in as fixture id 0/1 anywhere in the catalog.
    for entry in catalog.snapshot().values():
        assert all(type(fid) is int for fid in entry.fixtures)


# ---------------------------------------------------------------------------
# RED (b) — catalog lists real + synthetic with HONEST provenance + fixtures
# ---------------------------------------------------------------------------


def test_red_b_real_and_synthetic_listed_with_honest_provenance(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    curated.mkdir()
    _copy_real_pack(curated / "real")
    _make_synthetic_pack(tmp_path, "curated/synth", fixture_id=5)

    catalog = build_catalog(curated)

    real = catalog.get("real")
    synth = catalog.get("synth")
    assert real is not None and synth is not None

    # REAL pack: hash-verified, coherent genuine state.
    assert real.is_genuine is True
    assert real.provenance == GENUINE_TXLINE_PROVENANCE
    assert set(real.fixtures) == {18209181, 18213979, 18218149, 18222446}
    assert is_genuine_pack(real.pack_dir) is True

    # SYNTHETIC pack: listed, honestly labelled synthetic, NEVER genuine.
    assert synth.is_genuine is False
    assert synth.provenance == SYNTHETIC_PROVENANCE
    assert synth.provenance != GENUINE_TXLINE_PROVENANCE
    assert synth.fixtures == (5,)


def test_red_b_declared_genuine_but_incoherent_is_not_labelled_genuine(tmp_path: Path) -> None:
    """A pack that DECLARES genuine-txline but has a contradictory (synthetic=True) state must be
    hash-valid yet NEVER catalogued as genuine — proving the catalog trusts is_genuine_pack (coherent
    state), not the raw self-declared provenance string."""
    curated = tmp_path / "curated"
    curated.mkdir()
    liar = _make_synthetic_pack(tmp_path, "curated/liar", fixture_id=9)

    manifest = json.loads((liar / "pack.json").read_text())
    manifest["capture"]["provenance"] = GENUINE_TXLINE_PROVENANCE  # DECLARE genuine (but keep synthetic=True)
    # Re-seal so verify_content_hash PASSES — the tamper-evidence isn't what rejects genuineness here.
    manifest["content_hash"] = _compute_content_hash(
        liar, manifest["fixtures"], pack_version=int(manifest["pack_version"]), capture=manifest["capture"]
    )
    (liar / "pack.json").write_text(json.dumps(manifest))
    assert verify_content_hash(liar) is True  # hash verifies...
    assert is_genuine_pack(liar) is False  # ...but the state is NOT a coherent genuine one

    catalog = build_catalog(curated)
    entry = catalog.get("liar")
    assert entry is not None  # it IS catalogued (hash-valid)
    assert entry.is_genuine is False  # but NEVER as genuine
    assert entry.provenance != GENUINE_TXLINE_PROVENANCE  # fail-safe downgrade


# ---------------------------------------------------------------------------
# RED (c) — a NEW writable-capture-root pack hash-verifies + ATOMICALLY registers at RUNTIME
# ---------------------------------------------------------------------------


def test_red_c_new_capture_pack_registers_atomically_at_runtime(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    curated.mkdir()
    _make_synthetic_pack(tmp_path, "curated/seed", fixture_id=1)
    capture = tmp_path / "capture"
    capture.mkdir()

    catalog = build_catalog(curated, capture_root=capture)
    assert catalog.pack_ids() == ["seed"]

    # A snapshot taken BEFORE registration must not gain the new pack (copy-on-write / atomicity).
    before = catalog.snapshot()

    fresh = _make_synthetic_pack(tmp_path, "capture/fresh", fixture_id=42)
    entry = catalog.register_pack(fresh)

    assert entry.pack_id == "fresh"
    assert entry.fixtures == (42,)
    assert "fresh" in catalog  # promoted into the live catalog with NO restart
    assert catalog.get("seed") is not None  # existing entry preserved (no torn/partial swap)
    assert "fresh" not in before  # the pre-registration snapshot is unchanged (immutable COW)


def test_red_c_unverified_capture_pack_is_rejected(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    curated.mkdir()
    capture = tmp_path / "capture"
    capture.mkdir()
    catalog = build_catalog(curated, capture_root=capture)

    bad = _make_synthetic_pack(tmp_path, "capture/bad", fixture_id=13)
    _tamper_data_file(bad)  # now content_hash no longer verifies

    with pytest.raises(CatalogVerificationError):
        catalog.register_pack(bad)
    assert "bad" not in catalog  # REJECTED — an unverified pack is never promoted


# ---------------------------------------------------------------------------
# RED (d) — the READ-ONLY curated root is NEVER written
# ---------------------------------------------------------------------------


def test_red_d_curated_root_never_written(tmp_path: Path) -> None:
    curated = tmp_path / "curated"
    curated.mkdir()
    _copy_real_pack(curated / "real")
    _make_synthetic_pack(tmp_path, "curated/synth", fixture_id=5)
    capture = tmp_path / "capture"
    capture.mkdir()

    before = _dir_fingerprint(curated)

    catalog = build_catalog(curated, capture_root=capture)
    # Register a fresh capture pack (which must NOT touch the curated root).
    fresh = _make_synthetic_pack(tmp_path, "capture/fresh", fixture_id=99)
    catalog.register_pack(fresh)

    after = _dir_fingerprint(curated)
    assert before == after  # curated tree byte-and-mtime identical — the scan/register never wrote it


def test_red_d_register_refuses_curated_seed_pack_id_collision(tmp_path: Path) -> None:
    """A capture-root pack whose pack_id COLLIDES with a curated seed must NOT replace the genuine
    seed's catalog entry — the runtime writable-root promotion path can never overwrite a curated
    seed (load-bearing once R-3 exposes registration). A genuinely NEW pack_id still registers."""
    curated = tmp_path / "curated"
    curated.mkdir()
    # A GENUINE curated seed named "shared".
    _copy_real_pack(curated / "shared")
    capture = tmp_path / "capture"
    capture.mkdir()
    catalog = build_catalog(curated, capture_root=capture)
    seed_entry = catalog.get("shared")
    assert seed_entry is not None and seed_entry.is_genuine is True

    # A DIFFERENT (synthetic) pack under the writable capture root, colliding on pack_id "shared".
    collider = _make_synthetic_pack(tmp_path, "capture/shared", fixture_id=5)
    assert collider.name == "shared"

    with pytest.raises(CatalogAdmissionError):
        catalog.register_pack(collider)

    # The curated seed's entry is UNCHANGED — still the genuine one, never replaced.
    after = catalog.get("shared")
    assert after is seed_entry
    assert after.is_genuine is True
    assert after.provenance == GENUINE_TXLINE_PROVENANCE

    # A genuinely NEW pack_id still registers fine (the refusal is collision-specific, not blanket).
    fresh = _make_synthetic_pack(tmp_path, "capture/fresh", fixture_id=9)
    entry = catalog.register_pack(fresh)
    assert entry.pack_id == "fresh" and "fresh" in catalog


def test_red_d_register_refuses_curated_root_pack(tmp_path: Path) -> None:
    """Deployed capture must publish to the writable capture root — registering a curated-root pack is
    refused, so a curated seed can never be re-admitted via the writable promotion path."""
    curated = tmp_path / "curated"
    curated.mkdir()
    seed = _make_synthetic_pack(tmp_path, "curated/seed", fixture_id=3)
    capture = tmp_path / "capture"
    capture.mkdir()
    catalog = build_catalog(curated, capture_root=capture)

    with pytest.raises(ValueError, match="curated"):
        catalog.register_pack(seed)


# ---------------------------------------------------------------------------
# Blank / missing root -> empty catalog (fail-closed, not a crash)
# ---------------------------------------------------------------------------


def test_blank_root_yields_empty_catalog() -> None:
    assert isinstance(build_catalog(""), ReplayCatalog)
    assert len(build_catalog("")) == 0
    assert len(build_catalog(None)) == 0
