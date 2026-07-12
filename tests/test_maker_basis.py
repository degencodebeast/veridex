import pytest
from veridex.maker.basis import decompose_gap, reach_from_residual


def test_decompose_gap_rejects_length_mismatch():
    with pytest.raises(ValueError):
        decompose_gap([0.6, 0.6], [0.5])


def test_decompose_gap_rejects_empty():
    with pytest.raises(ValueError):
        decompose_gap([], [])


def test_structural_offset_is_reported_as_basis_not_edge():
    # constant 0.02 offset (TxLINE always 2c above venue) is pure basis, residual ~0
    fv = [0.60, 0.61, 0.62]; venue = [0.58, 0.59, 0.60]
    d = decompose_gap(fv, venue)
    assert d.basis_bps == 200 and all(abs(r) <= 1 for r in d.residual_gap_bps)


def test_convergence_shows_only_in_residual_after_basis_removed():
    # venue starts far then converges toward fv beyond the constant basis
    fv = [0.60, 0.60, 0.60]; venue = [0.50, 0.55, 0.585]
    d = decompose_gap(fv, venue)
    assert d.n == 3
    assert reach_from_residual(d.residual_gap_bps) is not None


def test_raw_gap_alone_cannot_be_called_edge():
    # the module exposes NO function that returns edge from the raw gap; only residual-based reach
    import veridex.maker.basis as b
    assert not hasattr(b, "edge_from_gap")
