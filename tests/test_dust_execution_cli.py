"""E7-T4 tests for the operator-only dust-execution CLI (SEC-005, AC-017/023, §6 group 11).

Offline and network-free. These tests drive the thin operator runbook CLI
(``scripts/maker/dust_execution.py``) and prove its five trust properties WITHOUT any live
venue/Privy/order/provisioning/credential-mint/real-signing call — Mode B stays UNARMED:

1. **Read-only status** — ``status`` reports session status (``configured: <bool>`` +
   closed-vocab telemetry) WITHOUT arming or submitting.
2. **Explicitly operator-authorized arm** — ``arm`` requires the explicit ``--i-am-operator``
   flag; without it the CLI never drives the facade (no arm, no I/O).
3. **Fail-closed secrets** — on missing signer creds the ``arm`` path RAISES (fail closed)
   BEFORE any venue/signer/Privy I/O; the recording seams record ZERO calls, and no secret
   VALUE ever reaches stdout — only ``configured: <bool>``.
4. **Signer-neutral, fail-closed** — swapping the signer provider preserves the CLI contract
   (``arm`` still drives the facade with ``arming=None`` / Mode B UNARMED), and an unconfigured
   provider fails closed before any I/O.
5. **Operator tool, NOT an agent surface** — the decision agent is assembled ``tools=[]`` and
   the CLI's proposer entry is never registered on it.

The offline seams (a recording :class:`FakeVenueAdapter`, a scripted quote source, a pinned
``EXPERIMENTAL_DUST`` manifest + policy envelope, and a Mode-A ``LocalFakeWalletControlPlane``)
mirror ``tests/test_dust_execution_facade.py``; Mode B is never armed (``arming=None``).
"""

from __future__ import annotations

import ast
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

import scripts.maker.dust_execution as cli
from veridex.dust_execution.facade import (
    MMExecutionToolRequest,
    MMExecutionToolResult,
    MMIntentParams,
    propose_mm_execution,
)
from veridex.dust_execution.manifest import StrategyExperimentManifest
from veridex.dust_execution.risk import FailClosed
from veridex.dust_execution.runner import BookSide, DustQuote
from veridex.dust_execution.signer import LocalFakeWalletControlPlane, Signer, SignerMode
from veridex.policy.envelope import PolicyEnvelope
from veridex.venues.sx_bet import FakeVenueAdapter

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CLI_PATH = _REPO_ROOT / "scripts" / "maker" / "dust_execution.py"

_MM_TOKEN = "0xtokenYES"
_MM_NOW_S = 1_700_000_000

# A KNOWN secret VALUE fixture — the CLI must NEVER echo it (SEC-005). If any of these substrings
# appears in stdout/stderr the no-secret-in-output guarantee is broken.
_SECRET_APP_SECRET = "PRIVY-APP-SECRET-DO-NOT-LEAK-abc123"
_SECRET_APP_ID = "PRIVY-APP-ID-LEAKY-xyz789"
_SECRET_WALLET_REF = "wallet-ref-SECRET-42"

_ALL_SECRETS = (_SECRET_APP_SECRET, _SECRET_APP_ID, _SECRET_WALLET_REF)


# --------------------------------------------------------------------------- offline seam builders


def _mm_manifest(**kw: object) -> StrategyExperimentManifest:
    base: dict[str, object] = {
        "strategy_id": "dust-maker-v0",
        "strategy_config_hash": "cfg" * 4,
        "evidence_class": "EXPERIMENTAL_DUST",
        "market": "0xcondition",
        "universe": (_MM_TOKEN,),
        "mode": "dry_run",
        "max_orders": 3,
        "max_notional": 5.0,
        "max_session_loss": 2.0,
        "max_daily_loss": 4.0,
        "session_window": (1_700_000_000_000, 1_700_000_600_000),
        "required_inputs": ("fair_value", "venue_book"),
        "permitted_intent_kinds": ("make",),
        "market_fee_snapshot_hash": "fee" * 4,
        "operator_authorization": "op-ref-1",
        "forbidden_claims": ("PROVEN_EDGE", "CALIBRATED"),
    }
    base.update(kw)
    return StrategyExperimentManifest(**base)  # type: ignore[arg-type]


def _mm_env(**kw: object) -> PolicyEnvelope:
    base: dict[str, object] = {
        "max_stake": 100.0,
        "max_orders_per_run": 5,
        "max_orders_per_session": 20,
        "max_orders_per_day": 50,
        "venue_allowlist": ["sx_bet"],
        "market_allowlist": ["0xcondition"],
        "min_edge_bps": 50,
        "max_slippage_bps": 100,
        "max_price": 3.0,
        "max_quote_age_s": 10,
        "cooldown_s": 0,
        "human_approval_threshold": 1000.0,
        "kill_switch": False,
    }
    base.update(kw)
    return PolicyEnvelope(**base)  # type: ignore[arg-type]


def _fresh_quote() -> DustQuote:
    return DustQuote(
        token_id=_MM_TOKEN,
        quote_ts_s=_MM_NOW_S,
        event_suspended=False,
        no_quote=False,
        bid=BookSide(price=0.49, size=10.0),
        ask=BookSide(price=0.51, size=10.0),
    )


class _ScriptedSource:
    """A recording-free injected quote source returning one scripted, gate-passing quote."""

    def __init__(self, quote: DustQuote) -> None:
        self._quote = quote
        self.reads: list[str] = []

    async def read_quote(self, token_id: str) -> DustQuote:
        self.reads.append(token_id)
        return self._quote


async def _noop_sleep(_seconds: float) -> None:
    return None


def _admitted_request(
    manifest: StrategyExperimentManifest, envelope: PolicyEnvelope
) -> MMExecutionToolRequest:
    """A sanctioned, hash-matched dry-run intent (the runner fails closed on any pin mismatch)."""
    return MMExecutionToolRequest.build(
        intent_kind="make_quote",
        intent_params=MMIntentParams(
            token_id=_MM_TOKEN, side="BUY", price=0.49, size=1.0, tif="GTC", client_order_id="coid-1"
        ),
        strategy_id=manifest.strategy_id,
        strategy_config_hash=manifest.strategy_config_hash,
        policy_hash=envelope.policy_hash(),
        session_id="sess-op-1",
        manifest_hash=manifest.manifest_hash(),
        evidence_class="EXPERIMENTAL_DUST",
        mode="dry_run",
        admitted_manifest_hash=manifest.manifest_hash(),
        admitted_policy_hash=envelope.policy_hash(),
        admitted_strategy_config_hash=manifest.strategy_config_hash,
    )


class _RecordingSigner:
    """A recording signer-provider stand-in: a Mode-A signer that counts its sign calls."""

    mode: SignerMode = "FAKE_LOCAL"

    def __init__(self) -> None:
        self.sign_calls = 0
        self._inner = LocalFakeWalletControlPlane()

    async def sign_order(self, payload: Any) -> Any:
        self.sign_calls += 1
        return await self._inner.sign_order(payload)


def _offline_session(
    *,
    adapter: FakeVenueAdapter,
    propose: Callable[..., Awaitable[MMExecutionToolResult]] = propose_mm_execution,
) -> cli.OperatorSession:
    """Build the injectable OFFLINE seam bundle the CLI drives the facade through (Mode-A dry_run)."""
    manifest = _mm_manifest()
    envelope = _mm_env()
    return cli.OperatorSession(
        request=_admitted_request(manifest, envelope),
        adapter=adapter,
        sources=_ScriptedSource(_fresh_quote()),
        envelope=envelope,
        manifest=manifest,
        now_fn=lambda: _MM_NOW_S,
        sleep_fn=_noop_sleep,
        wallet_equity_at_decision=100.0,
        fixed_fraction=0.01,
        propose=propose,
    )


class _RecordingPropose:
    """Records every facade drive so tests can assert IF and HOW the CLI drove the facade."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, request: MMExecutionToolRequest, **kwargs: Any
    ) -> MMExecutionToolResult:
        self.calls.append({"request": request, **kwargs})
        # A typed, honest result — never a raw handle (mirrors MMExecutionToolResult contract).
        return MMExecutionToolResult(
            admission="APPROVED",
            reason_codes=(),
            lifecycle_receipt_ref="dust-lifecycle:sess-op-1:deadbeefdeadbeef",
            run_label="DUST_LIVE",
            calibration_label="UNCALIBRATED",
            edge_label="NOT_PROVEN_EDGE",
            evidence_class="EXPERIMENTAL_DUST",
            policy_hash=request.policy_hash,
        )


def _privy_full_env() -> dict[str, str]:
    """A fully-configured Mode-B (Privy) env carrying the KNOWN secret fixtures."""
    return {
        "PRIVY_APP_ID": _SECRET_APP_ID,
        "PRIVY_APP_SECRET": _SECRET_APP_SECRET,
        "PRIVY_EXECUTION_WALLET_REF": _SECRET_WALLET_REF,
    }


def _assert_no_secret(text: str) -> None:
    for secret in _ALL_SECRETS:
        assert secret not in text, f"secret value {secret!r} leaked into CLI output"


# --------------------------------------------------------------------------- (3) fail-closed secrets


def test_arm_missing_creds_fails_closed_before_any_io(capsys: pytest.CaptureFixture[str]) -> None:
    """Absent signer creds → ``arm`` RAISES (fail closed) BEFORE any venue/signer/Privy I/O.

    We inject a recording session (recording adapter + recording ``propose``) and drive ``arm``
    with the explicit operator flag but an EMPTY env (no Privy creds). The CLI must fail closed at
    the credential guard BEFORE the facade is driven or the venue wire is touched — so the recording
    seams record ZERO calls, and no present secret ever reaches stdout.
    """
    adapter = FakeVenueAdapter(fill=True)
    recording = _RecordingPropose()
    session = _offline_session(adapter=adapter, propose=recording)

    with pytest.raises(FailClosed):
        cli.main(
            ["arm", "--signer", "privy_evm", "--i-am-operator"],
            env={},  # NO creds present
            session=session,
        )

    # Refuse-before-I/O: the facade was never driven and the venue wire was never touched.
    assert recording.calls == [], "arm must not drive the facade when signer creds are missing"
    assert adapter.submit_calls == 0, "no order may reach the venue wire on a fail-closed arm"

    out = capsys.readouterr()
    _assert_no_secret(out.out + out.err)


def test_status_reports_configured_bool_and_never_leaks_a_secret(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``status`` prints ``configured: <bool>`` closed-vocab telemetry and NEVER a secret value.

    With a fully-configured Privy env (carrying the known secret fixtures), ``status`` reports
    ``configured: true`` — but the raw secret VALUES must never appear in the output. Run again with
    an empty env: ``configured: false``, still no secret.
    """
    rc = cli.main(["status", "--signer", "privy_evm"], env=_privy_full_env())
    assert rc == 0
    configured_out = capsys.readouterr()
    assert "configured: true" in configured_out.out.lower()
    _assert_no_secret(configured_out.out + configured_out.err)

    rc = cli.main(["status", "--signer", "privy_evm"], env={})
    assert rc == 0
    unconfigured_out = capsys.readouterr()
    assert "configured: false" in unconfigured_out.out.lower()
    _assert_no_secret(unconfigured_out.out + unconfigured_out.err)


# --------------------------------------------------------------------------- (2) operator-authorized arm


def test_arm_requires_explicit_operator_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """Without ``--i-am-operator`` the CLI NEVER arms: no facade drive, no venue I/O (AC-023).

    Even with fully-configured creds, omitting the explicit operator authorization flag must make
    ``arm`` refuse (a non-zero return) WITHOUT driving the facade — arming is never implicit.
    """
    adapter = FakeVenueAdapter(fill=True)
    recording = _RecordingPropose()
    session = _offline_session(adapter=adapter, propose=recording)

    rc = cli.main(
        ["arm", "--signer", "privy_evm"],  # NO --i-am-operator
        env=_privy_full_env(),
        session=session,
    )

    assert rc != 0, "arm without explicit operator authorization must refuse (non-zero)"
    assert recording.calls == [], "arm must not drive the facade without the explicit operator flag"
    assert adapter.submit_calls == 0
    out = capsys.readouterr()
    _assert_no_secret(out.out + out.err)


def test_default_privy_arm_stays_unarmed_even_when_fully_configured(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Even fully-configured + explicitly-authorized, the DEFAULT build never arms Mode B (scope).

    With the default ``privy_evm`` provider, real creds present, and ``--i-am-operator`` set, the CLI
    still FAILS CLOSED: this runbook build wires NO live Mode-B Privy signer, so Mode B stays UNARMED.
    The facade is never driven and the venue wire is never touched — and no secret leaks. (The positive
    "arm drives the facade" path is covered by the ``fake_local`` real-facade and signer-swap tests.)
    """
    adapter = FakeVenueAdapter(fill=True)
    recording = _RecordingPropose()
    session = _offline_session(adapter=adapter, propose=recording)

    with pytest.raises(FailClosed):
        cli.main(
            ["arm", "--signer", "privy_evm", "--i-am-operator"],
            env=_privy_full_env(),
            session=session,
        )

    assert recording.calls == [], "the default build must not drive a live Mode-B arm"
    assert adapter.submit_calls == 0, "Mode B stays UNARMED — no order reaches the wire"
    out = capsys.readouterr()
    _assert_no_secret(out.out + out.err)


# --------------------------------------------------------------------------- (4) signer-neutral / fail-closed


def test_signer_provider_swap_preserves_contract(capsys: pytest.CaptureFixture[str]) -> None:
    """Swapping the signer provider preserves the CLI contract (arm still drives, Mode B UNARMED).

    We register a DIFFERENT provider (a recording Mode-A signer keyed on its own required env var)
    and select it. With its cred present the CLI arms exactly as with ``privy_evm``: it resolves the
    provider's signer and drives the facade with ``arming=None`` — the swap changes the signer, not
    the contract.
    """
    recording_signer = _RecordingSigner()
    provider = cli.SignerProvider(
        mode="FAKE_LOCAL",
        required_env_keys=("MY_SIGNER_CRED",),
        factory=lambda _env: recording_signer,
    )
    adapter = FakeVenueAdapter(fill=True)
    recording = _RecordingPropose()
    session = _offline_session(adapter=adapter, propose=recording)

    rc = cli.main(
        ["arm", "--signer", "myprov", "--i-am-operator"],
        env={"MY_SIGNER_CRED": "present"},
        session=session,
        signer_providers={"myprov": provider},
    )

    assert rc == 0
    assert len(recording.calls) == 1
    # The swapped provider's signer is the one driven into the facade (signer-neutral).
    assert recording.calls[0]["signer"] is recording_signer
    assert recording.calls[0].get("arming") is None
    out = capsys.readouterr()
    _assert_no_secret(out.out + out.err)


def test_unconfigured_signer_fails_closed_before_any_io(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An UNCONFIGURED signer provider fails closed BEFORE any I/O (signer-neutral fail-closed).

    We register a provider requiring a cred that is ABSENT from the env. ``arm`` (explicitly
    authorized) must raise ``FailClosed`` at the credential guard — before the facade is driven or
    the signer factory is invoked.
    """
    factory_called = {"n": 0}

    def _factory(_env: Any) -> Signer:
        factory_called["n"] += 1
        return LocalFakeWalletControlPlane()

    provider = cli.SignerProvider(
        mode="FAKE_LOCAL",
        required_env_keys=("MY_SIGNER_CRED",),
        factory=_factory,
    )
    adapter = FakeVenueAdapter(fill=True)
    recording = _RecordingPropose()
    session = _offline_session(adapter=adapter, propose=recording)

    with pytest.raises(FailClosed):
        cli.main(
            ["arm", "--signer", "myprov", "--i-am-operator"],
            env={},  # MY_SIGNER_CRED absent
            session=session,
            signer_providers={"myprov": provider},
        )

    assert factory_called["n"] == 0, "an unconfigured signer must not be built"
    assert recording.calls == [], "the facade must not be driven for an unconfigured signer"
    assert adapter.submit_calls == 0
    out = capsys.readouterr()
    _assert_no_secret(out.out + out.err)


# --------------------------------------------------------------------------- real-facade offline drive


def test_arm_drives_real_facade_offline_and_never_submits() -> None:
    """End-to-end: the CLI drives the REAL facade offline (Mode-A dry_run), Mode B UNARMED, 0 submits.

    Using the real ``propose_mm_execution`` and the offline seams, ``arm`` returns a typed
    ``MMExecutionToolResult`` with the honest labels and the injected recording-fake venue never sees
    a submit — proving the CLI genuinely drives R4-A through the facade without any live I/O.
    """
    adapter = FakeVenueAdapter(fill=True)
    captured: dict[str, MMExecutionToolResult] = {}

    async def _capturing_propose(
        request: MMExecutionToolRequest, **kwargs: Any
    ) -> MMExecutionToolResult:
        result = await propose_mm_execution(request, **kwargs)
        captured["result"] = result
        return result

    session = _offline_session(adapter=adapter, propose=_capturing_propose)

    rc = cli.main(
        ["arm", "--signer", "fake_local", "--i-am-operator"],
        env={},  # fake_local (Mode A) needs no creds
        session=session,
    )

    assert rc == 0
    result = captured["result"]
    assert isinstance(result, MMExecutionToolResult)
    assert result.admission == "APPROVED"
    assert result.run_label == "DUST_LIVE"
    assert result.edge_label == "NOT_PROVEN_EDGE"
    # Mode A dry_run stays offline: no order reached the injected recording-fake venue wire.
    assert adapter.submit_calls == 0


# --------------------------------------------------------------------------- (5) NOT an agent surface


def test_cli_is_not_an_agent_surface() -> None:
    """The operator CLI is a runbook tool, NOT an agent surface (tools=[] HARD invariant).

    The CLI source must not register itself on the ``tools=[]`` decision agent: it references no
    ``Agent(``/``tools=`` construction and does not import ``veridex.runtime.agent``. This keeps the
    execution authority OFF the decision agent (§6 group 11).
    """
    source = _CLI_PATH.read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert "veridex.runtime.agent" not in imported, "the runbook CLI must not import the decision agent"
    assert "Agent(" not in source, "the runbook CLI must not construct an agent"
    assert "tools=" not in source, "the runbook CLI must not register agent tools"


def test_cli_imports_no_venue_adapter_at_module_scope() -> None:
    """Offline-safe import: the CLI constructs no live venue/Privy client at module scope.

    It must import neither a concrete venue adapter (``veridex.venues.polymarket`` /
    ``veridex.venues.sx_bet``) nor the Mode-B Privy control plane at module scope — the live wire is
    reached only through an INJECTED session, never on import.
    """
    tree = ast.parse(_CLI_PATH.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    for forbidden in (
        "veridex.venues.polymarket",
        "veridex.venues.sx_bet",
        "veridex.dust_execution.privy_control_plane",
    ):
        assert forbidden not in imported, f"the runbook CLI must not import {forbidden} at module scope"
