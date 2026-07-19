"""R-1 — the banked GENUINE TxLINE seed ReplayPack: canonical-loader acceptance.

R-1 banks the genuine seed pack that bootstraps R-2's verified catalog and the deployment replay.
The seed already lives at ``scripts/fixtures/demo_pack_real/`` (banked by
``scripts/fixtures/build_demo_pack_real.py`` via the frozen
:func:`~veridex.ingest.replay_pack.pack_from_session` transform + R-0a's SEALED, CLOSED
``_genuine_backfill_authority`` capability). ``tests/test_demo_pack_real.py`` already exercises the
tamper-evidence + honesty trust surface (7 MAJOR-1 adversarial controls). This module adds the one
R-1 acceptance criterion those tests do NOT assert directly: that the SEED pack itself LOADS through
the CANONICAL pack loader (:func:`~veridex.ingest.replay_pack.load_pack_marketstates`) — the same
hash-gated, one-projection normalizer live TxLINE uses — for EVERY fixture in its manifest, while its
``content_hash`` verifies (recompute == stored) and it reads honestly genuine.

Offline, no network: a pure read of committed files through the trust-path loader.
"""

from __future__ import annotations

import json

from scripts.demo_phase2d import (
    DEMO_PACK_REAL_CONTENT_HASH,
    DEMO_PACK_REAL_DIR,
    _pack_content_hash,
)
from veridex.ingest.capture_chain import (
    GENUINE_TXLINE_PROVENANCE,
    is_genuine_pack,
    read_pack_provenance,
)
from veridex.ingest.replay_pack import load_pack_marketstates, verify_content_hash


def _manifest_fixture_ids() -> list[int]:
    """The fixture ids the banked seed pack's manifest declares (never guessed off filenames)."""
    manifest = json.loads((DEMO_PACK_REAL_DIR / "pack.json").read_text())
    return [entry["fixture_id"] for entry in manifest["fixtures"]]


def test_seed_pack_content_hash_recomputes_to_stored():
    """The banked seed's stored ``content_hash`` recomputes from its data files + authority block."""
    assert verify_content_hash(DEMO_PACK_REAL_DIR) is True
    # Stored hash == the demo-script pin (verify_content_hash above already proved recompute == stored).
    assert _pack_content_hash(DEMO_PACK_REAL_DIR) == DEMO_PACK_REAL_CONTENT_HASH


def test_seed_pack_reads_honestly_genuine():
    """provenance is the canonical genuine-TxLINE marker AND the coherent-genuine gate reads True."""
    assert read_pack_provenance(DEMO_PACK_REAL_DIR) == GENUINE_TXLINE_PROVENANCE
    assert is_genuine_pack(DEMO_PACK_REAL_DIR) is True


def test_seed_pack_loads_every_manifest_fixture_via_canonical_loader():
    """Every manifest fixture LOADS through the canonical, hash-gated loader into non-empty states.

    ``load_pack_marketstates(..., verify=True)`` re-checks ``content_hash`` on the LOAD path (refusing
    a tampered/relabeled pack) and feeds the raw records through the SAME normalizer live TxLINE uses —
    so a green load here proves the seed replays end-to-end, not merely that its hash is intact.
    """
    fixture_ids = _manifest_fixture_ids()
    assert fixture_ids, "seed pack manifest declares no fixtures"

    for fixture_id in fixture_ids:
        states = load_pack_marketstates(DEMO_PACK_REAL_DIR, fixture_id, verify=True)
        assert states, f"fixture {fixture_id} loaded no MarketStates from the seed pack"
        # The normalizer stamps a monotonic tick sequence per fixture — a coarse well-formedness check.
        assert [ms.tick_seq for ms in states] == list(range(len(states)))
