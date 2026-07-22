"""Tests for the immutable public-identity model (Official Replay League completion layer, A1).

These lock the HONESTY INVARIANT: ``owner_public_label`` must NEVER leak the raw ``owner_ref``
(a Privy DID / operator id). Official agents render a static brand; users render a shortened
wallet derived from the ref; anything else renders an em-dash.
"""

from __future__ import annotations

from veridex.public_agent import (
    OperatorClass,
    Origin,
    PublicAgent,
    Visibility,
    owner_public_label,
)

_TS = "2026-07-22T00:00:00Z"


def _agent(**overrides: object) -> PublicAgent:
    base: dict[str, object] = {
        "public_agent_id": "pa_1",
        "display_name": "Test Agent",
        "operator_class": OperatorClass.USER,
        "origin": Origin.UNKNOWN,
        "visibility": Visibility.PUBLIC,
        "owner_ref": None,
        "created_at": _TS,
        "updated_at": _TS,
    }
    base.update(overrides)
    return PublicAgent(**base)


def test_official_owner_label_static() -> None:
    agent = _agent(operator_class=OperatorClass.OFFICIAL, origin=Origin.OFFICIAL, owner_ref=None)
    assert owner_public_label(agent) == "Veridex Labs"


def test_user_owner_label_short_wallet_never_raw() -> None:
    raw = "did:privy:0x9cc2aaaaaaaaaaaaaaaaaaaaaaaa16ee3"
    agent = _agent(operator_class=OperatorClass.USER, owner_ref=raw)
    label = owner_public_label(agent)
    assert "did:privy" not in label
    assert raw not in label
    assert label.startswith("0x9cc2")
    assert "…" in label


def test_visibility_two_state_only() -> None:
    assert {v.value for v in Visibility} == {"private", "public"}


def test_origin_has_unknown_for_honest_legacy() -> None:
    assert Origin.UNKNOWN.value == "unknown"


def test_unknown_owner_ref_renders_dash() -> None:
    agent = _agent(operator_class=OperatorClass.USER, owner_ref=None)
    assert owner_public_label(agent) == "—"
