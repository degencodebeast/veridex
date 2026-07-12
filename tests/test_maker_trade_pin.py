"""E2-T1: trade_artifact_hash binds into config_hash + the R1 stamp is re-pinned.

Adding ``trade_artifact_hash: str | None = None`` to the frozen ``MakerRunConfig``
changes the no-artifact cp1 ``config_hash`` (pydantic v2 ``model_dump()`` includes
``None`` fields), so the R1 ``MAKER_EXPECTED_CONFIG_HASH`` stamp MUST be re-pinned to
the new value. These tests prove both the binding and that the re-pin is real.
"""

from veridex.maker.config import build_maker_run_config
from veridex.maker.runner import MAKER_EXPECTED_CONFIG_HASH

CP1_18 = (17588229, 17588234, 17588245, 17588325, 17588391, 17588404, 17926593, 18167317,
          18172280, 18172469, 18175918, 18175981, 18175983, 18176123, 18179550, 18179551, 18179759, 18179763)


def test_trade_artifact_hash_moves_config_hash():
    a = build_maker_run_config(fixture_ids=CP1_18).config_hash()  # no artifact

    class _FA:
        artifact_hash = "H1"

    class _FB:
        artifact_hash = "H2"

    b = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_FA()).config_hash()
    c = build_maker_run_config(fixture_ids=CP1_18, trade_artifact=_FB()).config_hash()
    assert a != b and b != c
    assert build_maker_run_config(fixture_ids=CP1_18).trade_artifact_hash is None


def test_no_artifact_config_hash_is_repinned():
    assert build_maker_run_config(fixture_ids=CP1_18).config_hash() == MAKER_EXPECTED_CONFIG_HASH
