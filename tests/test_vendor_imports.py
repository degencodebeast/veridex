"""T12: vendored Polymarket CLOB client — offline import + LOB sanity + hygiene (REQ-2D-201).

The vendor source is oracle-arb's BUNDLED quantpylib tree (self-contained, inline ``LOB`` —
no Cython ``hft/lob.pyx`` dependency), copied into ``veridex/venues/_vendor/polymarket_clob/``
under a neutral namespace. Importing must NOT require credentials or hit the network; the only
permitted edits vs. the upstream source are ``quantpylib.*`` -> vendored-namespace import rewrites.
"""

from __future__ import annotations

import subprocess
import sys

import numpy as np
import pytest


def test_import_lob_and_polymarket_offline() -> None:
    """Importing the vendored client must work with no creds and no network access."""
    from veridex.venues._vendor.polymarket_clob.client import LOB, Polymarket

    assert LOB is not None
    assert Polymarket is not None


def test_lob_get_mid_and_cumulative_size_on_synthetic_book() -> None:
    """LOB.get_mid / get_cumulative_size sanity on a tiny synthetic two-level book."""
    from veridex.venues._vendor.polymarket_clob.client import LOB

    book = LOB(depth=100, buffer_size=1)
    # columns: [price, size]; bids descending, asks ascending once sorted by update().
    bids = np.array([[0.40, 10.0], [0.39, 20.0]], dtype=np.float64)
    asks = np.array([[0.42, 15.0], [0.43, 25.0]], dtype=np.float64)
    book.update(timestamp=1234, bids=bids, asks=asks, is_snapshot=True, is_sorted=False)

    assert book.get_mid() == pytest.approx((0.40 + 0.42) / 2)

    size, notional = book.get_cumulative_size(dir=1, price=0.42)
    assert size == pytest.approx(15.0)
    assert notional == pytest.approx(0.42 * 15.0)


def test_no_dangling_quantpylib_imports_under_veridex() -> None:
    """Repo hygiene: no dangling `import quantpylib` reference anywhere under veridex/."""
    result = subprocess.run(
        ["grep", "-r", "import quantpylib", "veridex/"],
        cwd=None,
        capture_output=True,
        text=True,
    )
    # grep exit code 1 == no matches found (what we want); 0 == matches found (fail).
    assert result.returncode == 1, f"dangling quantpylib import(s) found:\n{result.stdout}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
