"""I-10 + Foundation-gate MAJOR-1 — the banked REAL demo pack: pinned, tamper-evident, PROVENANCE-HONEST.

The trust surface is PROVENANCE HONESTY. MAJOR-1 (this file's headline) closes the demonstrated
relabel bypass: the authority-bearing fields (``provenance``, ``test_capture``, ``synthetic``,
``evidence_rung``, ``capture_method``) are folded into a VERSIONED ``content_hash`` (pack_version 2),
so a post-build relabel of a SYNTHETIC pack to genuine is refused by ``verify_content_hash`` AND
``is_genuine_pack`` — the exact vector the controller reproduced. Existing v1 packs still LOAD but can
NEVER read genuine (their authority is not hash-bound). ``is_genuine_pack`` requires hash-verify
FIRST, then a coherent genuine state, and provenance authority is derived ONLY from a CLOSED set of
controller-owned producers (never an arbitrary source-supplied string).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import shutil
from pathlib import Path

import pytest

from scripts.demo_phase2d import (
    DEFAULT_PACK_DIR,
    DEMO_PACK_REAL_CONTENT_HASH,
    DEMO_PACK_REAL_DIR,
    SHARP_MOMENTUM_MIN_FIXTURES,
    _pack_content_hash,
    require_min_fixtures,
    resolve_real_demo_pack,
)
from scripts.txline_live_capture_accept import FakeStreamClient, odds_frames
from veridex.ingest.capture_chain import (
    GENUINE_TXLINE_PROVENANCE,
    TEST_FAKE_PROVENANCE,
    PackAuthority,
    _genuine_backfill_authority,
    _genuine_live_sse_authority,
    is_genuine_pack,
    read_pack_provenance,
    run_capture_chain,
    synthetic_authority,
)
from veridex.ingest.capture_chain import (
    test_fake_authority as make_test_fake_authority,
)
from veridex.ingest.recorder import SessionMeta, envelope_line
from veridex.ingest.replay_pack import _compute_content_hash, pack_from_session, verify_content_hash


def _write_session(session_dir: Path, records: list[dict]) -> None:
    """Write a minimal recorder session (meta + enveloped records) for the frozen packer."""
    session_dir.mkdir(parents=True, exist_ok=True)
    lines = [envelope_line(r, int(r["Ts"])) for r in records]
    (session_dir / "records.jsonl").write_text("\n".join(lines) + "\n")
    (session_dir / "meta.json").write_text(
        SessionMeta(started_ts=1, endpoints=[], tool_version="test").model_dump_json()
    )


def _odds_record(fixture_id: int, ts_ms: int) -> dict:
    return {
        "FixtureId": fixture_id,
        "Ts": ts_ms,
        "InRunning": False,
        "SuperOddsType": "OU",
        "MarketPeriod": None,
        "MarketParameters": "line=2.5",
        "PriceNames": ["Over", "Under"],
        "Prices": [1900, 2000],
        "Pct": [52.6, 47.4],
    }


def _build_pack(tmp_path: Path, authority: PackAuthority | None, name: str = "pack") -> Path:
    """Build a small ReplayPack under ``tmp_path/name`` with the given sealed authority capability."""
    session = tmp_path / f"session_{name}"
    pack_dir = tmp_path / name
    _write_session(session, [_odds_record(1, 100), _odds_record(1, 200)])
    pack_from_session(session, pack_dir, authority=authority)
    return pack_dir


#: The five hash-bound authority field names — used by the read-side fail-safe controls below.
_AUTHORITY_FIELD_NAMES = ("provenance", "test_capture", "synthetic", "evidence_rung", "capture_method")


def _build_self_consistent_pack_with_authority(tmp_path: Path, authority_fields: dict, name: str) -> Path:
    """Bank a v2 pack whose ``capture`` authority block is exactly ``authority_fields``, hash recomputed.

    Builds a legacy v1 pack (data-only), then folds ``authority_fields`` into a ``pack_version=2``
    ``capture`` block and RECOMPUTES ``content_hash`` so the pack stays internally self-consistent
    (``verify_content_hash`` passes). This lets a control assert the READ-side gate (``is_genuine_pack``)
    fail-safes on an incoherent-but-self-consistent authority block WITHOUT needing a sealed genuine
    capability (which the public API cannot mint) — it directly exercises the reader's own coherence check.
    """
    session = tmp_path / f"session_{name}"
    pack_dir = tmp_path / name
    _write_session(session, [_odds_record(1, 100), _odds_record(1, 200)])
    pack_from_session(session, pack_dir)  # v1, data-only hash, no authority fields in capture
    doc = json.loads((pack_dir / "pack.json").read_text())
    doc["pack_version"] = 2
    doc["capture"].update(authority_fields)  # add ONLY the (possibly-omitted) authority fields
    doc["content_hash"] = _compute_content_hash(
        pack_dir, doc["fixtures"], pack_version=2, capture=doc["capture"]
    )
    (pack_dir / "pack.json").write_text(json.dumps(doc))
    return pack_dir


# --- I-10 baseline: pinned pack verifies + content_hash MATCHES the demo-script pin --------------


def test_real_pack_verifies_content_hash():
    assert verify_content_hash(DEMO_PACK_REAL_DIR) is True


def test_real_pack_content_hash_matches_the_pin():
    # A mismatch (data mutated, authority relabeled, OR the pin edited without rebuilding) must
    # fail — the pin is the tamper-evidence contract binding the demo script to these banked bytes.
    assert _pack_content_hash(DEMO_PACK_REAL_DIR) == DEMO_PACK_REAL_CONTENT_HASH


def test_real_pack_tamper_is_detected(tmp_path: Path):
    copy = tmp_path / "tampered"
    shutil.copytree(DEMO_PACK_REAL_DIR, copy)
    manifest = json.loads((copy / "pack.json").read_text())
    victim = copy / manifest["fixtures"][0]["records"]
    victim.write_text(victim.read_text() + '{"FixtureId": 0, "Ts": 1, "Prices": [9999]}\n')
    assert verify_content_hash(copy) is False
    assert _pack_content_hash(copy) == DEMO_PACK_REAL_CONTENT_HASH  # stored value untouched...
    # ...but it no longer describes the (tampered) data files, which is exactly what verify catches.


def test_real_pack_reads_genuine_txline():
    assert read_pack_provenance(DEMO_PACK_REAL_DIR) == GENUINE_TXLINE_PROVENANCE
    assert is_genuine_pack(DEMO_PACK_REAL_DIR) is True


def test_real_pack_declares_backfill_evidence_rung_transparently():
    capture = json.loads((DEMO_PACK_REAL_DIR / "pack.json").read_text())["capture"]
    assert capture["evidence_rung"] == "backfilled-price-history"
    assert capture["test_capture"] is False


def test_synthetic_fallback_is_labeled_and_never_genuine():
    assert "synthetic" in read_pack_provenance(DEFAULT_PACK_DIR).lower()
    assert is_genuine_pack(DEFAULT_PACK_DIR) is False


def test_real_pack_exposes_at_least_two_fixtures():
    fixture_ids = require_min_fixtures(DEMO_PACK_REAL_DIR)
    assert len(fixture_ids) >= SHARP_MOMENTUM_MIN_FIXTURES
    assert len(set(fixture_ids)) == len(fixture_ids)


def test_single_fixture_pack_is_refused():
    with pytest.raises(ValueError, match="fixture"):
        require_min_fixtures(DEFAULT_PACK_DIR)


def test_resolver_returns_the_pinned_genuine_multi_fixture_pack():
    assert resolve_real_demo_pack() == DEMO_PACK_REAL_DIR


# ================================================================================================
# MAJOR-1 — the 7 required adversarial controls (Option 3 + closed-world structural authority)
# ================================================================================================


# --- Control 1: relabel the shipped synthetic pack to genuine WITHOUT recomputing --------------
def test_control1_relabel_without_recompute_fails_verify_and_genuine(tmp_path: Path):
    """The controller repro: copy the SYNTHETIC pack, stamp genuine authority, do NOT recompute.

    Because the authority block is folded into the v2 content_hash, verification AND genuineness
    both fail — closing the exact demonstrated bypass (previously BOTH read True).
    """
    copy = tmp_path / "relabeled"
    shutil.copytree(DEFAULT_PACK_DIR, copy)
    doc = json.loads((copy / "pack.json").read_text())
    doc["capture"]["provenance"] = GENUINE_TXLINE_PROVENANCE
    doc["capture"]["test_capture"] = False
    doc["capture"]["synthetic"] = False
    doc["capture"]["evidence_rung"] = "backfilled-price-history"
    doc["capture"]["capture_method"] = "odds-updates-backfill"
    (copy / "pack.json").write_text(json.dumps(doc))  # content_hash intentionally NOT recomputed
    assert verify_content_hash(copy) is False
    assert is_genuine_pack(copy) is False


# --- Control 2: relabel AND recompute -> new identity differs from the pin/registered join ------
def test_control2_relabel_and_recompute_diverges_from_pin(tmp_path: Path):
    """An attacker who ALSO recomputes gets a self-consistent pack — but a DIFFERENT content_hash,

    so it no longer matches the pinned/registered I-10 identity and is refused at that join. This
    is the honest scope: hash binding proves the authority declaration is unchanged since the
    identity was pinned; it does not stop someone who rewrites the code AND every pinned record.
    """
    copy = tmp_path / "relabeled_recomputed"
    shutil.copytree(DEFAULT_PACK_DIR, copy)
    doc = json.loads((copy / "pack.json").read_text())
    doc["pack_version"] = 2
    doc["capture"].update(_genuine_backfill_authority().as_capture_fields())
    doc["content_hash"] = _compute_content_hash(
        copy, doc["fixtures"], pack_version=2, capture=doc["capture"]
    )
    (copy / "pack.json").write_text(json.dumps(doc))
    # Self-consistent now (verify passes; reads genuine)...
    assert verify_content_hash(copy) is True
    assert is_genuine_pack(copy) is True
    # ...but the recomputed identity is NOT the pinned real-pack identity -> refused at the pin.
    assert doc["content_hash"] != DEMO_PACK_REAL_CONTENT_HASH


# --- Control 3: a custom fake CaptureSource declaring genuine cannot mint a genuine pack --------
def test_control3_custom_fake_source_cannot_mint_genuine(tmp_path: Path):
    """A hostile custom source that hardcodes ``provenance='genuine-txline'`` is NOT in the closed

    controller-owned producer set, so ``run_capture_chain`` derives test/unknown authority for it —
    the banked pack reads TEST, never genuine (authority is structural, not source-supplied).
    """

    class LyingSource:
        provenance = GENUINE_TXLINE_PROVENANCE  # the magic string a naive check would have trusted

        def credentials(self) -> tuple[str, str]:
            return ("jwt-x", "tok-x")

        def stream_client(self) -> FakeStreamClient:
            return FakeStreamClient(odds_frames(fixture_id=7, count=3))

    result = asyncio.run(
        run_capture_chain(LyingSource(), session_dir=tmp_path / "s", out_dir=tmp_path / "pack")
    )
    assert result.pack_dir is not None
    assert is_genuine_pack(result.pack_dir) is False
    assert read_pack_provenance(result.pack_dir) == TEST_FAKE_PROVENANCE


# --- Control 4 (D-residual): the PUBLIC generic builder cannot mint genuine from arbitrary records --
def test_control4_public_builder_cannot_mint_genuine_from_arbitrary_records(tmp_path: Path):
    """D-residual (Codex re-gate): the ordinary PUBLIC surface cannot mint a genuine pack from
    arbitrary data. There is NO public genuine-authority builder, constructing a genuine capability
    through the public class API fails closed, the open-dict input is gone, and feeding a synthetic
    session + the strongest publicly-obtainable authority never reads genuine.
    (RED before the fix: ``pack_from_session(<synthetic>, authority=genuine_backfill_authority())``
    yielded ``hash_verifies=True, is_genuine=True``.)
    """
    import veridex.ingest.capture_chain as cc

    # The old arbitrary-string minting helper AND the public genuine-authority builders are gone.
    assert not hasattr(cc, "stamp_pack_provenance")
    assert not hasattr(cc, "genuine_backfill_authority")
    assert not hasattr(cc, "genuine_live_sse_authority")
    # run_capture_chain still has NO provenance parameter (no injection seam).
    assert "provenance" not in inspect.signature(run_capture_chain).parameters

    # Constructing a genuine capability through the public class constructor fails closed (no seal).
    with pytest.raises(PermissionError):
        PackAuthority(
            provenance=GENUINE_TXLINE_PROVENANCE,
            test_capture=False,
            synthetic=False,
            evidence_rung="backfilled-price-history",
            capture_method="odds-updates-backfill",
        )

    # The open-dict authority input is gone — a raw dict (the old bypass) is rejected by the builder.
    session = tmp_path / "session_dict"
    _write_session(session, [_odds_record(1, 100), _odds_record(1, 200)])
    with pytest.raises((AttributeError, TypeError)):
        pack_from_session(
            session, tmp_path / "pack_dict", authority={"provenance": GENUINE_TXLINE_PROVENANCE}
        )

    # Feeding a synthetic session + the strongest PUBLICLY-obtainable authority never mints genuine.
    synth_pack = _build_pack(tmp_path, synthetic_authority(), name="pub_synth")
    assert verify_content_hash(synth_pack) is True
    assert is_genuine_pack(synth_pack) is False


#: The five genuine field VALUES an attacker wants folded into a pack's capture block — shared by the
#: three write-boundary bypass controls below (Codex re-review of the D-residual fix at 1adac64).
_FORGED_GENUINE_FIELDS: dict = {
    "provenance": GENUINE_TXLINE_PROVENANCE,
    "test_capture": False,
    "synthetic": False,
    "evidence_rung": "backfilled-price-history",
    "capture_method": "odds-updates-backfill",
}


# --- Control 4b (D-residual write boundary): a duck-typed object is refused ----------------------
def test_control4b_duck_typed_authority_object_is_refused(tmp_path: Path):
    """Codex re-review of the D-residual fix at commit 1adac64: the seal guarded ``PackAuthority``
    CONSTRUCTION but not the actual mint point. An arbitrary object exposing only
    ``as_capture_fields()`` — never a ``PackAuthority`` at all, so ``__post_init__``/the seal check
    never run — fed through ``pack_from_session`` must be refused OUTRIGHT (exact-type check).
    (RED before this fix: minted ``hash_verifies=True, is_genuine=True``.)
    """

    class DuckAuth:
        def as_capture_fields(self) -> dict:
            return dict(_FORGED_GENUINE_FIELDS)

    session = tmp_path / "session_duck"
    _write_session(session, [_odds_record(1, 100), _odds_record(1, 200)])
    pack_dir = tmp_path / "pack_duck"
    with pytest.raises(TypeError):
        pack_from_session(session, pack_dir, authority=DuckAuth())  # type: ignore[arg-type]
    # No pack.json was ever written -> is_genuine_pack fails safe (FileNotFoundError -> False).
    assert is_genuine_pack(pack_dir) is False


# --- Control 4c (D-residual write boundary): an object.__new__ bypass is refused -----------------
def test_control4c_object_new_bypass_is_refused(tmp_path: Path):
    """``object.__new__(PackAuthority)`` constructs a REAL ``PackAuthority`` WITHOUT running
    ``__init__``/``__post_init__`` — the seal is never checked, and genuine field values can be forced
    on via ``object.__setattr__`` (frozen dataclasses only block the NORMAL ``__setattr__`` path). This
    passes the exact-type check but must fail the second guard: PROVEN seal possession (``_sealed``,
    set ONLY inside ``__post_init__``) is absent here.
    (RED before this fix: minted ``hash_verifies=True, is_genuine=True``.)
    """
    forged = object.__new__(PackAuthority)
    for field, value in _FORGED_GENUINE_FIELDS.items():
        object.__setattr__(forged, field, value)

    session = tmp_path / "session_forged"
    _write_session(session, [_odds_record(1, 100), _odds_record(1, 200)])
    pack_dir = tmp_path / "pack_forged"
    with pytest.raises(PermissionError):
        pack_from_session(session, pack_dir, authority=forged)
    assert is_genuine_pack(pack_dir) is False


# --- Control 4d (D-residual write boundary): a `_claims_genuine`-override subclass is refused -----
def test_control4d_claims_genuine_override_subclass_is_refused(tmp_path: Path):
    """A ``PackAuthority`` SUBCLASS overriding ``_claims_genuine`` to always return ``False`` neuters a
    construction-time-only check (``__post_init__`` calls ``self._claims_genuine()``, which dynamically
    dispatches to the override) while still carrying genuine field values. The write-boundary guard's
    EXACT-type membership (``type(authority) is PackAuthority``, mirroring the F-residual fix) rejects
    ANY subclass outright — the overridden method is never even consulted for a non-exact type.
    (RED before this fix: minted ``hash_verifies=True, is_genuine=True``.)
    """

    class Sneaky(PackAuthority):
        def _claims_genuine(self) -> bool:
            return False

    # Construction itself succeeds (the override defeats the construction-time check) — proving the
    # write boundary, not construction, must be the authoritative gate.
    sneaky = Sneaky(**_FORGED_GENUINE_FIELDS)

    session = tmp_path / "session_sneaky"
    _write_session(session, [_odds_record(1, 100), _odds_record(1, 200)])
    pack_dir = tmp_path / "pack_sneaky"
    with pytest.raises(TypeError):
        pack_from_session(session, pack_dir, authority=sneaky)
    assert is_genuine_pack(pack_dir) is False


# --- Control 5: genuine live SSE and genuine verified backfill -> DISTINCT rungs, both verify ---
def test_control5_live_and_backfill_are_distinct_genuine_rungs(tmp_path: Path):
    # The two CLOSED genuine producers (sealed capabilities) — reached via the module-private
    # constructors that only trusted producer paths call.
    live = _genuine_live_sse_authority()
    backfill = _genuine_backfill_authority()
    assert live.evidence_rung == "recorded-live-quote"
    assert backfill.evidence_rung == "backfilled-price-history"
    assert live.evidence_rung != backfill.evidence_rung

    live_pack = _build_pack(tmp_path, live, name="live")
    backfill_pack = _build_pack(tmp_path, backfill, name="backfill")
    for pack in (live_pack, backfill_pack):
        assert verify_content_hash(pack) is True
        assert is_genuine_pack(pack) is True


# --- Control 6: any incoherent authority -> is_genuine_pack False (fail-safe) --------------------
@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda a: a.update(test_capture=True), id="test_capture_true"),
        pytest.param(lambda a: a.update(synthetic=True), id="synthetic_true"),
        pytest.param(lambda a: a.pop("provenance"), id="missing_provenance"),
        pytest.param(lambda a: a.pop("evidence_rung"), id="missing_evidence_rung"),
        # E-residual: a hash-valid v2 authority block with `synthetic` OMITTED must fail closed.
        pytest.param(lambda a: a.pop("synthetic"), id="missing_synthetic"),
        pytest.param(lambda a: a.update(evidence_rung="synthetic"), id="non_genuine_rung"),
        pytest.param(lambda a: a.update(capture_method="totally-unknown"), id="unknown_method"),
        pytest.param(lambda a: a.update(provenance="synthetic-illustrative"), id="contradictory_provenance"),
    ],
)
def test_control6_incoherent_authority_never_reads_genuine(tmp_path: Path, mutate):
    """A pack whose authority block is self-consistent (verify passes) but INCOHERENT for a genuine

    capture must never read genuine — test_capture, synthetic, missing/contradictory fields, or an
    unrecognized rung/method all fail-safe to False. The read-side gate (``is_genuine_pack``) is
    exercised directly against a self-consistent pack whose capture block carries the mutated fields.
    """
    authority_fields = _genuine_backfill_authority().as_capture_fields()
    mutate(authority_fields)
    pack = _build_self_consistent_pack_with_authority(tmp_path, authority_fields, name="incoherent")
    # The pack is internally self-consistent (its own hash verifies)...
    assert verify_content_hash(pack) is True
    # ...but the incoherent authority can never read genuine.
    assert is_genuine_pack(pack) is False


# --- Control 7: the re-banked I-10 pack verifies at its NEW pin; identity joins use that hash ----
def test_control7_rebanked_real_pack_is_v2_and_verifies_at_new_pin():
    doc = json.loads((DEMO_PACK_REAL_DIR / "pack.json").read_text())
    assert doc["pack_version"] == 2  # re-banked under the authority-inclusive hash semantics
    assert verify_content_hash(DEMO_PACK_REAL_DIR) is True
    assert _pack_content_hash(DEMO_PACK_REAL_DIR) == DEMO_PACK_REAL_CONTENT_HASH
    assert is_genuine_pack(DEMO_PACK_REAL_DIR) is True
    # The fail-closed resolver (the single honest entrypoint) accepts it at the new pin.
    assert resolve_real_demo_pack() == DEMO_PACK_REAL_DIR


# --- v1/v2 boundary: existing v1 packs still LOAD but can NEVER read genuine (fail-safe) ---------
def test_v1_pack_loads_but_never_reads_genuine(tmp_path: Path):
    """A v1 pack (no authority folded into the hash) keeps loading, but its unbound provenance can

    never make it read genuine — even when its ``capture`` block is hand-stamped genuine.
    """
    session = tmp_path / "s_v1"
    pack = tmp_path / "v1_pack"
    _write_session(session, [_odds_record(1, 100), _odds_record(1, 200)])
    pack_from_session(session, pack)  # NO authority -> pack_version 1, data-only hash (legacy)
    doc = json.loads((pack / "pack.json").read_text())
    assert doc["pack_version"] == 1
    # Hand-stamp genuine authority into the v1 capture block (NOT hash-bound in v1).
    doc["capture"]["provenance"] = GENUINE_TXLINE_PROVENANCE
    doc["capture"]["test_capture"] = False
    doc["capture"]["synthetic"] = False
    doc["capture"]["evidence_rung"] = "backfilled-price-history"
    doc["capture"]["capture_method"] = "odds-updates-backfill"
    (pack / "pack.json").write_text(json.dumps(doc))
    # v1 data-only hash still matches (authority isn't hashed in v1) -> the pack still LOADS...
    assert verify_content_hash(pack) is True
    # ...but a v1 pack can NEVER read genuine, because its authority is not hash-bound (fail-safe).
    assert is_genuine_pack(pack) is False


# --- test-fake / synthetic authority helpers stay non-genuine (defense in depth) ----------------
def test_test_fake_and_synthetic_authorities_are_never_genuine(tmp_path: Path):
    fake_pack = _build_pack(tmp_path, make_test_fake_authority(), name="fake")
    synth_pack = _build_pack(tmp_path, synthetic_authority(), name="synth")
    assert verify_content_hash(fake_pack) is True
    assert verify_content_hash(synth_pack) is True
    assert is_genuine_pack(fake_pack) is False
    assert is_genuine_pack(synth_pack) is False
    assert read_pack_provenance(fake_pack) == TEST_FAKE_PROVENANCE
    assert "synthetic" in read_pack_provenance(synth_pack).lower()
