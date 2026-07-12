"""Operator-only dust-execution runbook CLI (E7-T4, SEC-005, AC-017/023, §6 group 11).

A THIN operator entrypoint over the E7-T1 facade (:func:`veridex.dust_execution.facade.
propose_mm_execution`). It exposes exactly two operator verbs:

* ``status`` — a READ-ONLY session-status report. It prints closed-vocab telemetry
  (``configured: <bool>``, the selected signer mode, ``mode_b_armed: false``) and NEVER a secret
  value. It neither arms nor submits.
* ``arm`` — the explicitly operator-authorized execution path. It REFUSES unless the operator
  passes ``--i-am-operator`` (arming is never implicit, AC-023), it FAILS CLOSED on missing signer
  creds (raises BEFORE any venue/signer/Privy I/O, SEC-005), and — because this runbook build keeps
  Mode B UNARMED (scope) — it drives the facade with ``arming=None``. It never constructs a raw
  venue/signer call of its own; it drives R4-A THROUGH the facade.

Trust discipline (mirrors ``scripts/maker/live_recorder.py`` and ``veridex/config.py``):

* **Boundary — operator tool, NOT an agent surface.** This module imports NEITHER
  ``veridex.runtime.agent`` NOR any concrete venue adapter / Privy control plane; it constructs no
  decision agent and registers no callable tool. The decision agent's empty-tool-list invariant is
  HARD and this runbook stays off it (§6 group 11).
* **Fail-closed secrets (SEC-005).** :class:`SignerProvider` mirrors ``veridex.config``'s
  fail-fast-at-use pattern: :meth:`SignerProvider.require` raises BEFORE the signer is built (and
  before the facade is driven) when a required credential is absent, and only the boolean
  :meth:`SignerProvider.configured` flag — never a secret VALUE — is ever printed. Any guard message
  is scrubbed via :func:`_scrub`.
* **Signer-neutral.** The signer is resolved through an injectable :class:`SignerProvider` registry;
  swapping the provider preserves the CLI contract and an unconfigured provider fails closed.
* **Offline-safe / injectable.** The live wire is reached ONLY through an injected
  :class:`OperatorSession` (the facade drive + its offline seams). The default build wires NO live
  venue session, so a bare invocation cannot reach a live venue — Mode B stays UNARMED.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from veridex.dust_execution.facade import (
    MMExecutionToolRequest,
    MMExecutionToolResult,
    propose_mm_execution,
)
from veridex.dust_execution.manifest import StrategyExperimentManifest
from veridex.dust_execution.risk import FailClosed
from veridex.dust_execution.signer import (
    LocalFakeWalletControlPlane,
    Signer,
    SignerMode,
)
from veridex.policy.envelope import PolicyEnvelope

__all__ = [
    "OperatorSession",
    "SignerProvider",
    "build_parser",
    "default_signer_providers",
    "main",
]

#: Required Mode-B (Privy) signer env keys — resolved fail-closed at ARM time, never printed.
_PRIVY_REQUIRED_ENV: tuple[str, ...] = (
    "PRIVY_APP_ID",
    "PRIVY_APP_SECRET",
    "PRIVY_EXECUTION_WALLET_REF",
)


def _scrub(text: str, *secrets: str) -> str:
    """Redact each secret VALUE from *text* before it is printed (mirrors the live-recorder scrub)."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


# --------------------------------------------------------------------------- signer-neutral provider


@dataclass(frozen=True)
class SignerProvider:
    """A provider-neutral signer descriptor: its required creds + a fail-closed builder (SEC-005).

    ``required_env_keys`` are the env vars the provider needs to be configured; a Mode-A offline
    provider declares none (always configured). :meth:`configured` is boolean-only telemetry — it
    NEVER exposes a value — and :meth:`require` fails closed (raises :class:`FailClosed`) BEFORE the
    signer ``factory`` runs when any required cred is absent. Swapping the provider preserves the CLI
    contract; an unconfigured provider fails closed.
    """

    mode: SignerMode
    required_env_keys: tuple[str, ...]
    factory: Callable[[Mapping[str, str]], Signer]

    def configured(self, env: Mapping[str, str]) -> bool:
        """Whether EVERY required cred is present — boolean-only, never a secret VALUE."""
        return all(bool(env.get(key)) for key in self.required_env_keys)

    def require(self, env: Mapping[str, str]) -> Signer:
        """Build the signer only when fully configured, else FAIL CLOSED before the factory runs.

        The credential check runs BEFORE :attr:`factory` is invoked, so a missing cred raises
        (refuse-before-I/O) without ever building the signer. The message names only the ABSENT env
        KEYS — never a value.
        """
        missing = [key for key in self.required_env_keys if not env.get(key)]
        if missing:
            raise FailClosed(
                f"signer {self.mode} not configured: set {', '.join(self.required_env_keys)} "
                f"(absent: {', '.join(missing)})"
            )
        return self.factory(env)


def _build_privy_signer_unavailable(_env: Mapping[str, str]) -> Signer:
    """Default Mode-B factory: refuse — this runbook build wires NO live Privy signer (Mode B UNARMED).

    Reached only when the operator supplies real Privy creds AND the default provider is used; it
    fails closed rather than construct a live signing client (no live Privy I/O in this build). An
    operator drives an offline arm by injecting a signer provider + an :class:`OperatorSession`.
    """
    raise FailClosed(
        "Mode-B live Privy signer wiring is intentionally NOT enabled in this runbook build (E7-T4): "
        "Mode B stays UNARMED. Inject a SignerProvider + OperatorSession to drive an offline arm."
    )


def default_signer_providers() -> dict[str, SignerProvider]:
    """The default signer-provider registry (signer-neutral; the arm target is Mode-B Privy).

    ``privy_evm`` is the real-money arm target (fails closed without its creds AND, in this build,
    refuses to construct a live signer). ``fake_local`` is the Mode-A offline signer (no creds), used
    for an offline arm rehearsal that never reaches a live wire.
    """
    return {
        "privy_evm": SignerProvider(
            mode="PRIVY_EVM",
            required_env_keys=_PRIVY_REQUIRED_ENV,
            factory=_build_privy_signer_unavailable,
        ),
        "fake_local": SignerProvider(
            mode="FAKE_LOCAL",
            required_env_keys=(),
            factory=lambda _env: LocalFakeWalletControlPlane(),
        ),
    }


# --------------------------------------------------------------------------- injectable facade drive


@dataclass(frozen=True)
class OperatorSession:
    """The injectable OFFLINE seam bundle the CLI drives the facade through (never a live wire).

    Carries the sanctioned admitted :class:`MMExecutionToolRequest` plus the facade's injected seams
    (venue adapter, quote source, policy envelope, pinned manifest, clock/sleep seams, mechanical
    sizing inputs) and the ``propose`` callable (default :func:`~veridex.dust_execution.facade.
    propose_mm_execution`). The CLI drives this with ``arming=None`` so Mode B stays UNARMED. The
    default CLI wires NO session, so a bare invocation cannot reach a live venue.
    """

    request: MMExecutionToolRequest
    adapter: object
    sources: object
    envelope: PolicyEnvelope
    manifest: StrategyExperimentManifest
    now_fn: Callable[[], int]
    sleep_fn: Callable[[float], Awaitable[None]]
    wallet_equity_at_decision: float
    fixed_fraction: float
    propose: Callable[..., Awaitable[MMExecutionToolResult]] = field(default=propose_mm_execution)

    async def drive(self, signer: Signer) -> MMExecutionToolResult:
        """Drive R4-A THROUGH the facade with the resolved ``signer`` and ``arming=None`` (UNARMED)."""
        return await self.propose(
            self.request,
            adapter=self.adapter,
            signer=signer,
            sources=self.sources,
            now_fn=self.now_fn,
            sleep_fn=self.sleep_fn,
            envelope=self.envelope,
            manifest=self.manifest,
            wallet_equity_at_decision=self.wallet_equity_at_decision,
            fixed_fraction=self.fixed_fraction,
            arming=None,  # Mode B stays UNARMED (E7-T4 scope) — never a real-money arm
        )


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    """Build the operator argument parser (constructs NO live source — offline-safe, ``--help`` works)."""
    parser = argparse.ArgumentParser(
        prog="dust_execution",
        description=(
            "Operator-only dust-execution runbook: read-only status + an explicitly "
            "operator-authorized arm (fail-closed secrets; Mode B stays UNARMED)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser(
        "status", help="READ-ONLY session status (configured flag + signer mode); never arms/submits."
    )
    status.add_argument("--signer", default="privy_evm", help="signer provider to report (default: privy_evm)")

    arm = sub.add_parser(
        "arm", help="Explicitly operator-authorized arm; fail-closed on missing creds; Mode B UNARMED."
    )
    arm.add_argument("--signer", default="privy_evm", help="signer provider to arm (default: privy_evm)")
    arm.add_argument(
        "--i-am-operator",
        action="store_true",
        dest="i_am_operator",
        help="REQUIRED explicit operator authorization — arming is never implicit (AC-023).",
    )
    return parser


def _resolve_provider(
    signer_name: str, providers: Mapping[str, SignerProvider]
) -> SignerProvider:
    """Look up the selected signer provider, or fail closed on an unknown name."""
    provider = providers.get(signer_name)
    if provider is None:
        known = ", ".join(sorted(providers)) or "<none>"
        raise FailClosed(f"unknown signer provider {signer_name!r} (known: {known})")
    return provider


def _run_status(
    args: argparse.Namespace,
    env: Mapping[str, str],
    providers: Mapping[str, SignerProvider],
) -> int:
    """READ-ONLY: report the session status as closed-vocab telemetry — never a secret, never arms."""
    provider = _resolve_provider(args.signer, providers)
    # Boolean-only telemetry (SEC-005): the CONFIGURED flag, never a credential value.
    print(f"signer: {provider.mode}")
    print(f"configured: {str(provider.configured(env)).lower()}")
    print("mode_b_armed: false")
    print("mode: read_only_status")
    return 0


def _run_arm(
    args: argparse.Namespace,
    env: Mapping[str, str],
    providers: Mapping[str, SignerProvider],
    session: OperatorSession | None,
) -> int:
    """Explicitly-authorized arm: refuse without the flag, fail closed on creds, then drive the facade.

    Order is load-bearing (refuse-before-I/O): (1) require the explicit operator flag; (2) resolve
    the signer provider and FAIL CLOSED on missing creds BEFORE anything else; (3) require an injected
    session; (4) drive R4-A THROUGH the facade with ``arming=None`` (Mode B UNARMED).
    """
    # (1) EXPLICIT operator authorization — arming is never implicit (AC-023).
    if not args.i_am_operator:
        print("arm refused: pass --i-am-operator to explicitly authorize (arming is never implicit)")
        return 2

    # (2) FAIL CLOSED on missing signer creds — raises BEFORE any venue/signer/Privy I/O (SEC-005).
    provider = _resolve_provider(args.signer, providers)
    signer = provider.require(env)  # raises FailClosed when unconfigured (before the session is used)

    # (3) The live wire is reached ONLY through an injected session; without one, fail closed.
    if session is None:
        raise FailClosed(
            "arm refused: no execution session wired — this runbook build constructs no live venue "
            "session. Inject an OperatorSession to drive R4-A offline (Mode B stays UNARMED)."
        )

    # (4) Drive R4-A THROUGH the facade (never a raw venue/signer call), Mode B UNARMED.
    result = asyncio.run(session.drive(signer))

    print(f"signer: {provider.mode}")
    print(f"configured: {str(provider.configured(env)).lower()}")
    print(f"admission: {result.admission}")
    print(f"run_label: {result.run_label}")
    print(f"edge_label: {result.edge_label}")
    print("mode_b_armed: false")
    return 0


def main(
    argv: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    session: OperatorSession | None = None,
    signer_providers: Mapping[str, SignerProvider] | None = None,
) -> int:
    """Parse ``argv`` and run the operator verb (status | arm).

    ``env`` defaults to ``os.environ`` and is injectable so the fail-closed guards are drivable
    offline. ``session`` and ``signer_providers`` are injectable seams; the defaults wire NO live
    venue session and a Mode-B-refusing Privy provider, so a bare invocation cannot reach a live
    wire (Mode B stays UNARMED). No secret VALUE is ever echoed.
    """
    env = os.environ if env is None else env
    providers = default_signer_providers() if signer_providers is None else signer_providers
    args = build_parser().parse_args(argv)

    try:
        if args.command == "status":
            return _run_status(args, env, providers)
        return _run_arm(args, env, providers, session)
    except FailClosed:
        # Re-raise fail-closed as the caller's contract (tests assert the raise); the message names
        # only env KEYS, never a secret VALUE, but scrub defensively against any present cred value.
        raise
    except ValueError as exc:  # any downstream guard surfacing a value error — scrub before exit
        raise SystemExit(_scrub(str(exc), *(env.get(key, "") for key in _PRIVY_REQUIRED_ENV))) from None


if __name__ == "__main__":  # pragma: no cover - operator entry point
    raise SystemExit(main())
