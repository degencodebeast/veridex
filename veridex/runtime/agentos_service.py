"""AgentOS service composition + the AC-27 deny-by-default trust boundary (II-4; the CRITICAL boundary).

Hosts the II-2 market-maker run inside AgentOS behind a Veridex-owned, deny-by-default boundary that
keeps the reviewed Privy principal + persisted ``AgentInstance.operator_id`` as the sole authority
(Codex B+; Approach A rejected). The composition is:

1. Build the Veridex FastAPI app (``create_app``) — ALL veridex routes register FIRST.
2. Register the Veridex owner-scoped wrapper routes on it: ``POST /agents/instances/{instance_id}/runs``
   and ``POST /agents/instances/{instance_id}/runs/{run_id}/cancel`` — authenticate + owner-gate the
   PERSISTED instance, acquire the lease, and drive the adapter with SERVER-pre-allocated ids.
3. Snapshot the veridex route table (these self-gate via FastAPI ``Depends``).
4. Compose ``AgentOS(agents=[adapter], db=owner_db, base_app=veridex_app,
   on_route_conflict="preserve_base_app")`` and call ``get_app()`` ONCE.
5. Run the AC-29 deployed-surface contract (fail-closed on agno drift — never a silent fallback).
6. Wrap the FINAL composed app in the OUTER deny-by-default ASGI guard covering BOTH ``http`` AND
   ``websocket`` scopes: veridex routes pass (self-gated); EVERY agno-native route is denied (401 on
   anonymous, 403 on authenticated) — no agno-native run/session/cancel/workflow-ws route is public.

The single adapter means ownership can never come from a caller-supplied ``instance_id`` / session
metadata: the wrapper resolves it from server-owned state only (the Gate-4 forgeable-seam is closed).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from starlette.routing import compile_path

from veridex.api.auth_privy import PrivyPrincipal, make_require_principal, verify_privy_token
from veridex.api.router import create_app
from veridex.runtime.mm_agent_adapter import OwnerMismatchError, VeridexAgentAdapter
from veridex.store import DuplicateLeaseError, InstanceLease, LeaseStatus

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from fastapi import APIRouter

    from veridex.api.auth_privy import _Verifier
    from veridex.config import Settings
    from veridex.ingest.replay_catalog import ReplayCatalog
    from veridex.runtime.runtime_events import RuntimeEventSink
    from veridex.store import Store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AC-29 — the pinned agno-native surface (deny-by-default policy inventory)
# ---------------------------------------------------------------------------

#: The Veridex owner-scoped wrapper routes (registered on the base app; they self-gate via Depends).
VERIDEX_WRAPPER_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/agents/instances/{instance_id}/runs"),
        ("POST", "/agents/instances/{instance_id}/runs/{run_id}/cancel"),
    }
)

#: The REQUIRED agno-native run/cancel/session routes AC-29 asserts still exist (drift guard). If any
#: of these disappears or changes method/template on an agno upgrade, deploy FAILS (never a silent
#: custom-route fallback). They are all DENIED publicly by the guard — this asserts their SHAPE only.
REQUIRED_AGNO_NATIVE_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/agents/{agent_id}/runs"),
        ("POST", "/agents/{agent_id}/runs/{run_id}/cancel"),
        ("POST", "/agents/{agent_id}/runs/{run_id}/continue"),
        ("POST", "/agents/{agent_id}/sessions/{session_id}/fork"),
        ("GET", "/agents/{agent_id}/runs/{run_id}"),
        ("GET", "/sessions"),
        ("POST", "/sessions"),
        ("GET", "/sessions/{session_id}"),
        ("DELETE", "/sessions/{session_id}"),
        ("PATCH", "/sessions/{session_id}"),
        ("POST", "/sessions/{session_id}/rename"),
        ("WEBSOCKET", "/workflows/ws"),
    }
)


#: The EXACT public routes a caller-supplied ``base_router`` is permitted to expose. A base router is
#: captured in the veridex matcher snapshot and therefore BYPASSES the outer deny-by-default guard, so
#: the composition constrains it to this HARDCODED allowlist (currently only the no-auth ``/readyz``
#: readiness probe — it exposes no owner/instance/competition data) and FAILS STARTUP on any other
#: method/path. Authority is established here in code, NEVER by a docstring warning or a caller-supplied
#: allowlist / dependency introspection.
_ALLOWED_PUBLIC_BASE_ROUTES: frozenset[tuple[str, str]] = frozenset({("GET", "/readyz")})


class AgentOSCompositionError(RuntimeError):
    """Raised when the DEPLOYED AgentOS surface drifts from the reviewed policy (AC-29 — fail deploy).

    Fail-closed: the service refuses to compose rather than silently exposing an unreviewed agno-native
    route (a new run/session/workflow route, a shadowed veridex endpoint, or a required route that
    disappeared / changed method or template).
    """


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(tz=UTC).isoformat()


def _normalize_path(path: str) -> str:
    """Strip a trailing slash the way agno's ``TrailingSlashMiddleware`` does (so both forms match)."""
    if path == "/":
        return path
    return path.rstrip("/") or "/"


def _is_cors_preflight(scope: dict[str, Any]) -> bool:
    """Whether ``scope`` is a CORS preflight — an ``OPTIONS`` carrying ``Access-Control-Request-Method``.

    Per the Fetch/CORS spec a preflight is issued by the browser BEFORE the real request, is
    mandatorily credential-less (no cookies/Authorization), and performs no side effect — it only
    asks whether the real request's method+headers are allowed. It MUST NOT be auth-gated: a 401 here
    fails the *real* request as an opaque CORS error (``net::ERR_FAILED``) in the browser, even though
    the real request is properly authenticated. Exempting it preserves deny-by-default for every
    non-preflight request; the app's ``CORSMiddleware`` answers the preflight downstream.
    """
    if scope.get("type") != "http" or scope.get("method", "").upper() != "OPTIONS":
        return False
    return any(key == b"access-control-request-method" for key, _ in scope.get("headers", []))


@dataclass(frozen=True)
class _RouteMatcher:
    """A compiled (method-set, path-regex) matcher for one route template."""

    methods: frozenset[str] | None  # None => a websocket route (no HTTP method)
    regex: Any  # compiled path regex from starlette.compile_path
    template: str

    def matches(self, method: str | None, path: str, is_websocket: bool) -> bool:
        """Whether ``(method, path)`` hits this route template (path already trailing-slash-normalized)."""
        if is_websocket != (self.methods is None):
            return False
        if self.regex.match(path) is None:
            return False
        if self.methods is None:
            return True  # websocket: method-agnostic
        return method is not None and method.upper() in self.methods


def _walk_routes(routes: Any, prefix: str = "") -> list[tuple[frozenset[str] | None, str]]:
    """Recursively flatten a route tree into ``(methods|None, full_path)`` entries.

    Handles FastAPI's lazy ``_IncludedRouter`` wrapper (routes live on ``original_router`` under
    ``include_context.prefix``) and Starlette ``Mount`` sub-apps, so the ENTIRE composed surface is
    enumerated (a shallow ``app.routes`` scan misses agno's included routers). ``methods`` is ``None``
    for a websocket route.
    """
    from fastapi.routing import APIRoute, APIWebSocketRoute
    from starlette.routing import Mount

    out: list[tuple[frozenset[str] | None, str]] = []
    for route in routes:
        type_name = type(route).__name__
        if isinstance(route, APIRoute):
            out.append((frozenset(route.methods or set()), prefix + route.path))
        elif isinstance(route, APIWebSocketRoute):
            out.append((None, prefix + route.path))
        elif type_name == "_IncludedRouter":
            ctx = route.include_context
            out.extend(_walk_routes(route.original_router.routes, prefix + (ctx.prefix or "")))
        elif isinstance(route, Mount):
            out.extend(_walk_routes(getattr(route, "routes", []), prefix + getattr(route, "path", "")))
    return out


def _route_table(app: FastAPI) -> list[tuple[str, str]]:
    """Enumerate the composed route table as ``(METHOD, path_template)`` pairs (recursively flattened).

    Each HTTP route contributes one pair per method; a websocket route contributes ``WEBSOCKET``.
    """
    table: list[tuple[str, str]] = []
    for methods, path in _walk_routes(app.routes):
        if methods is None:
            table.append(("WEBSOCKET", path))
        else:
            for method in sorted(methods):
                table.append((method, path))
    return table


def _router_route_table(router: APIRouter) -> list[tuple[str, str]]:
    """Enumerate an ``APIRouter``'s contributed ``(METHOD, path_template)`` pairs (websocket => WEBSOCKET).

    Used to validate a caller-supplied ``base_router`` against :data:`_ALLOWED_PUBLIC_BASE_ROUTES`
    BEFORE it is included on the base app (so a non-allowlisted public route fails startup).
    """
    table: list[tuple[str, str]] = []
    for methods, path in _walk_routes(router.routes):
        if methods is None:
            table.append(("WEBSOCKET", path))
        else:
            for method in sorted(methods):
                table.append((method, path))
    return table


def _matchers_for(app: FastAPI) -> list[_RouteMatcher]:
    """Compile a matcher for every HTTP/websocket route currently on ``app`` (recursively flattened)."""
    matchers: list[_RouteMatcher] = []
    for methods, path in _walk_routes(app.routes):
        regex, _fmt, _conv = compile_path(path)
        matchers.append(_RouteMatcher(methods, regex, path))
    return matchers


# ---------------------------------------------------------------------------
# Layer 1 — the OUTER deny-by-default ASGI guard (authN over http AND websocket)
# ---------------------------------------------------------------------------


class DenyByDefaultGuard:
    """The OUTERMOST ASGI guard: veridex routes pass (self-gated); every agno-native route is DENIED.

    A pure ASGI callable (NOT a ``BaseHTTPMiddleware``, which never sees ``websocket`` scopes). It
    inspects both ``scope["type"] == "http"`` and ``"websocket"``. For a request that does NOT match a
    veridex-owned route it authenticates first (401 on anonymous — closing every native run/session/
    cancel/workflow-ws surface to an unauthenticated caller) and then denies (403) — no agno-native
    route is ever public. Non-http/websocket scopes (lifespan) pass through untouched.
    """

    def __init__(
        self,
        app: Any,
        *,
        veridex_matchers: list[_RouteMatcher],
        settings: Settings,
        verifier: _Verifier,
    ) -> None:
        """Wrap ``app`` (the FINAL composed app) with the deny-by-default boundary.

        Args:
            app: The composed AgentOS/FastAPI ASGI app to guard.
            veridex_matchers: Compiled matchers for the veridex-owned routes (captured BEFORE agno
                added its routes) — these pass through and self-gate via their FastAPI dependencies.
            settings: Resolved settings (auth mode + Privy verifier material).
            verifier: The Privy token verifier (injectable for tests).
        """
        self.app = app
        self._veridex = veridex_matchers
        self._require_principal = make_require_principal(settings, verifier)

    def _is_veridex(self, method: str | None, path: str, is_websocket: bool) -> bool:
        return any(m.matches(method, path, is_websocket) for m in self._veridex)

    def _authenticate(self, scope: dict[str, Any]) -> PrivyPrincipal | None:
        """Run the SAME ``require_principal`` verification used by the veridex routes (401 -> None)."""
        authorization: str | None = None
        for key, value in scope.get("headers", []):
            if key == b"authorization":
                authorization = value.decode("latin-1")
                break
        try:
            return self._require_principal(authorization=authorization)
        except HTTPException:
            return None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        is_websocket = scope_type == "websocket"
        path = _normalize_path(scope.get("path", ""))
        method = None if is_websocket else scope.get("method", "GET")

        # 0) CORS preflight -> pass through UNAUTHENTICATED so the app's CORSMiddleware answers it.
        # A preflight is OPTIONS + Access-Control-Request-Method; the route matchers are method-exact
        # (e.g. only POST for /agents/deploy), so a preflight would otherwise fall through to the
        # deny-by-default 401 below and break the *real* authenticated request as a browser CORS error.
        # This is spec-safe: preflights are credential-less and perform no action (see _is_cors_preflight).
        if _is_cors_preflight(scope):
            await self.app(scope, receive, send)
            return

        # 1) Veridex-owned route -> pass through; its own FastAPI Depends is authoritative.
        if self._is_veridex(method, path, is_websocket):
            await self.app(scope, receive, send)
            return

        # 2) agno-native (or unknown): authenticate FIRST (401 on anonymous), then DENY (403).
        principal = self._authenticate(scope)
        if principal is None:
            await self._reject(scope, receive, send, 401, "authentication required")
            return
        await self._reject(scope, receive, send, 403, "route not permitted")

    async def _reject(
        self, scope: dict[str, Any], receive: Any, send: Any, status: int, detail: str
    ) -> None:
        """Send a fail-closed rejection over the correct protocol (http response OR websocket close)."""
        if scope.get("type") == "websocket":
            # Reject before accepting the handshake — the connection can never execute.
            await receive()  # consume websocket.connect
            code = 1008 if status == 403 else 1008  # policy violation for both anon + non-owner
            await send({"type": "websocket.close", "code": code})
            return
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# Layer 2 — the owner-scoped Veridex wrapper routes (authZ from server-owned state only)
# ---------------------------------------------------------------------------


class StartRunRequest(BaseModel):
    """Body for the wrapper run-start route. Carries NO ownership signal (never trusted for identity)."""

    input: Any | None = None


class StartRunResponse(BaseModel):
    """Response for a wrapper run-start: the SERVER-pre-allocated ids + terminal status."""

    instance_id: str
    run_id: str
    session_id: str
    status: str


class CancelRunResponse(BaseModel):
    """Response for a wrapper cancel: the run's phase + whether THIS call engaged the exactly-once kill."""

    instance_id: str
    run_id: str
    phase: str
    engaged: bool


async def _load_owned_instance(store: Store, instance_id: str, principal: PrivyPrincipal) -> Any:
    """Load ``instance_id`` and apply the SAME owner gate as ``deploy.py`` (404 absent/unowned, 403 other)."""
    try:
        instance = await store.get_agent_instance(instance_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="agent instance not found") from exc
    if instance.operator_id is None:
        raise HTTPException(status_code=404, detail="agent instance not found")
    if instance.operator_id != principal.did:
        raise HTTPException(status_code=403, detail="principal does not own this agent instance")
    return instance


async def start_owned_instance_run(
    store: Store,
    adapter: VeridexAgentAdapter,
    *,
    instance: Any,
    run_id: str,
    session_id: str,
    input: Any = None,
    event_sink: RuntimeEventSink | None = None,
) -> Any:
    """The SHARED lease + runtime_handle + adapter-drive service (II-5 req 1).

    Owns exactly the sequence the II-4 wrapper originally inlined: atomically claim the
    single-active-run lease (STARTING) → persist the instance ``runtime_handle`` → advance the lease
    to ACTIVE → drive the adapter inline → terminal lease transition (RELEASED on success, FAILED on
    any exception — cancellation-safe). BOTH the owner-scoped wrapper route
    (``POST /agents/instances/{instance_id}/runs``) and the ``quoteguard-mm`` deploy dispatch call
    THIS function — never an in-process HTTP call to the wrapper, never a cloned lease state machine.

    Takes an EXPLICIT ``run_id`` / ``session_id`` — this function mints NEITHER (the requirement-2
    fix at the shared-logic level: the caller is the sole run-identity authority). The wrapper route
    mints its own per-call ids (unchanged II-4 behavior); the deploy dispatch passes the persisted
    ``AgentInstance.run_id`` so instance/lease/runtime_handle/RunContext/OPS/receipt/response all
    share ONE authoritative identity (II-5 requirement 2).

    Args:
        store: The Veridex store (instance + lease persistence).
        adapter: The hosted :class:`VeridexAgentAdapter` to drive the run through.
        instance: The ALREADY owner-verified, persisted ``AgentInstance`` (ownership resolution is the
            caller's job — the wrapper's ``Depends``-gated owner check, or the deploy saga's
            server-derived principal; this function trusts ``instance.operator_id`` as given).
        run_id / session_id: The caller-supplied, server-pre-allocated run identity.
        input: Opaque run input forwarded to the adapter (never an ownership signal).
        event_sink: OPS sink for this run.

    Returns:
        Whatever the adapter's :meth:`~VeridexAgentAdapter.start_run` returns.

    Raises:
        DuplicateLeaseError: The instance already holds an active lease (the caller maps this to a
            409 at an HTTP boundary, or a controlled failure status in a background saga).
    """
    import contextlib

    runtime_agent_id = adapter.get_id()
    now = _now_iso()

    # (1) Atomically claim the single-active-run lease in STARTING (INSERT-only, UNIQUE(instance_id)).
    lease = InstanceLease(
        instance_id=instance.instance_id,
        runtime_agent_id=runtime_agent_id,
        session_id=session_id,
        run_id=run_id,
        status=LeaseStatus.STARTING,
        operator_id=instance.operator_id,  # AUDIT only — authority stays on the instance
        created_at=now,
        updated_at=now,
    )
    # Let DuplicateLeaseError propagate — the caller maps it to its own boundary's semantics
    # (409 for the wrapper's HTTP request; a controlled FAILED status for a background saga).
    await store.acquire_instance_lease(lease)

    # (2)+(3) Persist the runtime_handle + advance the lease to ACTIVE, then drive the run inline.
    try:
        instance.runtime_handle = {
            "runtime_kind": "agentos",
            "runtime_agent_id": runtime_agent_id,
            "session_id": session_id,
            "run_id": run_id,
        }
        await store.persist_agent_instance(instance)
        await store.release_instance_lease(instance.instance_id, LeaseStatus.ACTIVE, updated_at=_now_iso())
        result = await adapter.start_run(
            run_id=run_id,
            session_id=session_id,
            runtime_agent_id=runtime_agent_id,
            owner_did=instance.operator_id,
            input=input,
            event_sink=event_sink,
        )
    except BaseException:
        # (4) Cancellation-safe fail: a client disconnect (CancelledError) or error never leaves an
        # unclassified lease. Idempotent CAS makes this safe even if a later RELEASED also runs.
        with contextlib.suppress(Exception):
            await store.release_instance_lease(instance.instance_id, LeaseStatus.FAILED, updated_at=_now_iso())
        raise
    # (4) Success: idempotent RELEASED.
    await store.release_instance_lease(instance.instance_id, LeaseStatus.RELEASED, updated_at=_now_iso())
    return result


def _register_wrapper_routes(
    app: FastAPI,
    *,
    store: Store,
    adapter: VeridexAgentAdapter,
    require_principal: Callable[..., PrivyPrincipal],
    event_sink: RuntimeEventSink | None,
    surface_only: bool = False,
) -> None:
    """Register the owner-scoped run-start + cancel wrapper routes on ``app`` (BEFORE agno composition).

    When ``surface_only`` is ``True`` (the served host composition — see
    :func:`~veridex.api.server.create_server_app`), the routes are still REGISTERED (so the AC-29
    wrapper-route contract and native surface stay byte-identical) but they DENY the request BEFORE any
    lease / ``runtime_handle`` mutation: this composition hosts the AgentOS SURFACE only and is NOT the
    per-instance executor (the real per-instance run path stays authority-bound in ``deploy.py``). A
    surface-only refusal therefore leaves ZERO durable footprint — never a partially-mutated instance.
    """

    @app.post("/agents/instances/{instance_id}/runs", response_model=StartRunResponse)
    async def start_instance_run(
        instance_id: str,
        body: StartRunRequest | None = None,
        principal: PrivyPrincipal = Depends(require_principal),  # noqa: B008
    ) -> StartRunResponse:
        """Start ONE run for an owned instance under a crash-safe lease + SERVER-pre-allocated ids.

        Ownership comes ONLY from the PERSISTED ``AgentInstance.operator_id`` (never the request). The
        lease's ``UNIQUE(instance_id)`` guarantees exactly one active run — a concurrent/duplicate
        starter gets 409 and NEVER launches a second run. Delegates the lease/handle/adapter sequence
        to the SHARED :func:`start_owned_instance_run` service (II-5 req 1).
        """
        if surface_only:
            # SURFACE-ONLY served host: refuse BEFORE any store/lease/runtime_handle mutation. The
            # served composition hosts the AgentOS surface but cannot execute a per-instance run
            # (per-instance execution is authority-bound via deploy.py). Deny cleanly — no corruption.
            raise HTTPException(
                status_code=409,
                detail=(
                    "served surface is not the per-instance executor; per-instance runs are "
                    "authority-bound via the deploy path"
                ),
            )
        instance = await _load_owned_instance(store, instance_id, principal)

        # SERVER-pre-allocate the run identity — never caller-supplied.
        run_id = f"run_{uuid4().hex}"
        session_id = f"sess_{uuid4().hex}"

        try:
            await start_owned_instance_run(
                store,
                adapter,
                instance=instance,
                run_id=run_id,
                session_id=session_id,
                input=body.input if body is not None else None,
                event_sink=event_sink,
            )
        except DuplicateLeaseError as exc:
            # Already running (or mid-STARTING after a crash): NEVER a second run — 409, bounded.
            raise HTTPException(status_code=409, detail="instance already has an active run") from exc
        return StartRunResponse(
            instance_id=instance_id, run_id=run_id, session_id=session_id, status="completed"
        )

    @app.post(
        "/agents/instances/{instance_id}/runs/{run_id}/cancel", response_model=CancelRunResponse
    )
    async def cancel_instance_run(
        instance_id: str,
        run_id: str,
        principal: PrivyPrincipal = Depends(require_principal),  # noqa: B008
    ) -> CancelRunResponse:
        """Cancel a run for an owned instance — owner-gated BEFORE any effect, exactly-once engage.

        Ownership resolves from server-owned state (the persisted instance + the lease's ``run_id``),
        never from caller-supplied identity. The adapter re-checks the owner before any kill effect.
        """
        if surface_only:
            # SURFACE-ONLY served host: refuse before any effect (see start_instance_run above).
            raise HTTPException(
                status_code=409,
                detail=(
                    "served surface is not the per-instance executor; run cancellation is "
                    "authority-bound via the deploy path"
                ),
            )
        instance = await _load_owned_instance(store, instance_id, principal)
        lease = await store.get_instance_lease(instance_id)
        if lease is None or lease.run_id != run_id:
            # run/lease coherence — a run_id that is not this instance's active run is 404 (no leak).
            raise HTTPException(status_code=404, detail="run not found for this instance")
        try:
            result = await adapter.acancel_run(run_id, owner_did=instance.operator_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except OwnerMismatchError as exc:  # defense in depth (owner already gated above)
            raise HTTPException(status_code=403, detail="principal does not own this run") from exc
        return CancelRunResponse(
            instance_id=instance_id, run_id=run_id, phase=result.phase.value, engaged=result.engaged
        )


# ---------------------------------------------------------------------------
# AC-29 — deployed-surface contract (fail-closed on drift)
# ---------------------------------------------------------------------------


def assert_adapter_contract(adapter: VeridexAgentAdapter) -> None:
    """Assert the adapter exposes the agno hooks + cancel capability (AC-29 signature guard).

    Raises:
        AgentOSCompositionError: If a required adapter method is missing.
    """
    for method in ("_arun_adapter", "_arun_adapter_stream", "arun", "acancel_run", "start_run"):
        if not callable(getattr(adapter, method, None)):
            raise AgentOSCompositionError(f"adapter missing required method: {method!r}")


def assert_agentos_contract() -> None:
    """Assert ``AgentOS.__init__`` still accepts db/agents/base_app/on_route_conflict='preserve_base_app'.

    Raises:
        AgentOSCompositionError: If the AgentOS constructor drifted from the validated I-9 shape.
    """
    import inspect

    from agno.os import AgentOS

    sig = inspect.signature(AgentOS.__init__)
    for param in ("db", "agents", "base_app", "on_route_conflict"):
        if param not in sig.parameters:
            raise AgentOSCompositionError(f"AgentOS.__init__ missing param: {param!r}")
    annotation = str(sig.parameters["on_route_conflict"].annotation)
    if "preserve_base_app" not in annotation:
        raise AgentOSCompositionError("AgentOS on_route_conflict no longer offers 'preserve_base_app'")


def agno_native_routes(composed: FastAPI, veridex_templates: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Return the composed routes that are agno-native (present after composition, not veridex-owned)."""
    return {entry for entry in _route_table(composed) if entry not in veridex_templates}


def assert_agno_surface(composed: FastAPI, veridex_templates: set[tuple[str, str]]) -> None:
    """Validate the DEPLOYED agno-native surface against the reviewed policy (AC-29 — fail deploy).

    Fails closed if: a required agno-native run/cancel/session/workflow-ws route disappeared or changed
    method/template; the veridex wrapper routes are shadowed / missing; or a NEW agno-native route
    appeared that no reviewer classified. Every agno-native route remains DENIED publicly by the guard;
    this contract forces a human to re-review the policy on any agno upgrade.

    Raises:
        AgentOSCompositionError: On any drift from the reviewed surface.
    """
    native = agno_native_routes(composed, veridex_templates)

    # (a) required agno-native routes must still exist (none disappeared / changed shape).
    missing = REQUIRED_AGNO_NATIVE_ROUTES - native
    if missing:
        raise AgentOSCompositionError(f"required agno-native routes missing/changed: {sorted(missing)}")

    # (b) the veridex wrapper routes must be present + win (never shadowed by agno's /agents/... route).
    composed_all = set(_route_table(composed))
    wrapper_missing = VERIDEX_WRAPPER_ROUTES - composed_all
    if wrapper_missing:
        raise AgentOSCompositionError(f"veridex wrapper routes shadowed/missing: {sorted(wrapper_missing)}")

    # (c) no UNCLASSIFIED agno-native route may appear — a new one fails deploy for human review.
    unknown = native - _KNOWN_AGNO_NATIVE_ROUTES
    if unknown:
        raise AgentOSCompositionError(
            f"unclassified NEW agno-native routes appeared (deny-by-default policy needs review): "
            f"{sorted(unknown)}"
        )


# ---------------------------------------------------------------------------
# Composition entrypoint
# ---------------------------------------------------------------------------


def build_agentos_app(
    *,
    store: Store,
    settings: Settings,
    adapter: VeridexAgentAdapter,
    owner_db: Any,
    verifier: _Verifier = verify_privy_token,
    event_sink: RuntimeEventSink | None = None,
    enforce_contract: bool = True,
    extra_agents: Sequence[VeridexAgentAdapter] | None = None,
    base_routers: Sequence[APIRouter] | None = None,
    surface_only: bool = False,
    replay_catalog: ReplayCatalog | None = None,
) -> Any:
    """Compose the guarded AgentOS app: veridex routes + adapter(s) + deny-by-default boundary.

    Args:
        store: The Veridex :class:`~veridex.store.Store` (instance records + leases).
        settings: Resolved settings (auth mode + Privy verifier material).
        adapter: The PRIMARY hosted :class:`VeridexAgentAdapter` (the MM adapter; the owner-scoped
            wrapper routes drive THIS adapter).
        owner_db: The agno owner-scoped db injected into AgentOS (e.g. ``agno.db.in_memory.InMemoryDb``).
        verifier: Privy token verifier (injectable for tests).
        event_sink: Optional OPS sink threaded into runs.
        enforce_contract: When ``True`` (default) run the AC-29 deployed-surface contract and FAIL
            (raise) on drift. Tests toggle it to observe drift explicitly.
        extra_agents: ADDITIVE (II-8b) — further hosted adapters (the directional contestants) added
            to the ``AgentOS(agents=[...])`` composition so their agno-native run/cancel routes exist
            behind the SAME deny-by-default boundary. Agno's agent routes are templated by
            ``{agent_id}``, so extra agents add NO new route templates — the AC-29 native surface is
            unchanged. Each is contract-checked. Defaults to ``None`` (byte-identical to before).
        base_routers: ADDITIVE — extra ``APIRouter``s (currently only the deploy ``/readyz`` readiness
            probe) registered on the base app BEFORE the veridex matcher snapshot, so they are
            subtracted as veridex-owned by the AC-29 contract (they add NO agno-native surface).
            Defaults to ``None`` (byte-identical to before).

            **SECURITY — a base router BYPASSES deny-by-default, so it is CONSTRAINED, not trusted.**
            Because it is captured in the veridex matcher snapshot, every route it declares PASSES the
            guard unauthenticated. This is therefore NOT a general extension hook: each route a base
            router contributes MUST appear in the hardcoded :data:`_ALLOWED_PUBLIC_BASE_ROUTES`
            allowlist (currently exactly the no-auth ``GET /readyz`` probe, which exposes no
            owner/instance/competition data). A base router carrying ANY other method/path FAILS
            STARTUP (raises :class:`AgentOSCompositionError`) — authority is enforced in code here, not
            by dependency introspection or a caller-supplied allowlist.
        surface_only: When ``True`` (the served host composition), the owner-scoped wrapper routes are
            still registered (AC-29 wrapper contract + native surface unchanged) but DENY before any
            lease / ``runtime_handle`` mutation — this composition hosts the AgentOS SURFACE only and
            is NOT the per-instance executor (that stays authority-bound in ``deploy.py``). Defaults to
            ``False`` (the wrapper genuinely drives the adapter — the test-harness / non-served path).
        replay_catalog: ADDITIVE — the ALREADY-BUILT, authoritative R-2 :class:`ReplayCatalog` the served
            composition built ONCE (in ``create_server_app``). Threaded straight into ``create_app`` so the
            served path builds the hash-verified catalog exactly ONCE — NOT a second env-built throwaway.
            Passing it also removes the latent trust hazard of the served ``app.state.replay_catalog``
            depending on statement ordering + agno's in-place base-app mutation to overwrite a divergent,
            ``os.environ``-built catalog. ``None`` (default) keeps the standalone behaviour: ``create_app``
            env-builds its own catalog (backward-compatible for direct ``build_agentos_app`` callers/tests).

    Returns:
        The OUTERMOST :class:`DenyByDefaultGuard` ASGI app wrapping the composed FastAPI app.

    Raises:
        AgentOSCompositionError: If a base router exposes a non-allowlisted route (fail-closed at
            startup), or the AC-29 contract fails (drifted agno surface).
    """
    from agno.os import AgentOS

    hosted_agents: list[VeridexAgentAdapter] = [adapter, *(extra_agents or [])]

    # (1) veridex routes FIRST. Thread the SERVER's already-built catalog through so the served path
    # builds the hash-verified R-2 catalog ONCE (no env-built throwaway, no double copytree, no reliance
    # on statement-ordering to overwrite a divergent catalog). None -> create_app env-builds its own.
    veridex_app = create_app(store=store, settings=settings, replay_catalog=replay_catalog)
    # (1a) additive base routers (currently only the /readyz probe) — on the base app BEFORE the
    # snapshot so they are veridex-owned/self-gated (they PASS the guard), never agno-native + denied.
    # SECURITY: because such a route bypasses the guard, each one MUST be in the hardcoded public
    # allowlist; anything else FAILS STARTUP (no unchecked auth bypass via a caller-supplied router).
    for router in base_routers or ():
        for entry in _router_route_table(router):
            if entry not in _ALLOWED_PUBLIC_BASE_ROUTES:
                raise AgentOSCompositionError(
                    "base_routers may expose ONLY the hardcoded public allowlist "
                    f"{sorted(_ALLOWED_PUBLIC_BASE_ROUTES)}; got un-permitted route {entry}. A base "
                    "router bypasses the deny-by-default guard, so the served surface is constrained "
                    "to the allowlist (fail-closed at startup)."
                )
        veridex_app.include_router(router)
    require_principal = make_require_principal(settings, verifier)

    # (2) owner-scoped wrapper routes on the base app (BEFORE composition). The wrapper drives the
    # PRIMARY adapter only; directional contestants are hosted via agno-native routes + start_run. On
    # the served surface-only host (surface_only=True) the routes are registered but deny before any
    # mutation (the served composition is not the per-instance executor).
    _register_wrapper_routes(
        veridex_app,
        store=store,
        adapter=adapter,
        require_principal=require_principal,
        event_sink=event_sink,
        surface_only=surface_only,
    )

    # (3) snapshot the veridex surface (self-gated) BEFORE agno mutates the base app.
    veridex_matchers = _matchers_for(veridex_app)
    veridex_templates = set(_route_table(veridex_app))

    # (4) compose + get_app() ONCE (preserve_base_app keeps the veridex routes authoritative).
    os_app = AgentOS(
        id="veridex-agentos",
        # agno's BaseExternalAgent types ``id`` as Optional[str]; our adapters always set it (and
        # __post_init__ guarantees non-None), so they structurally satisfy AgentProtocol at runtime.
        agents=hosted_agents,  # type: ignore[arg-type]
        db=owner_db,
        base_app=veridex_app,
        on_route_conflict="preserve_base_app",
        telemetry=False,
    )
    composed = os_app.get_app()

    # (5) AC-29 — fail-closed on any agno-surface drift (never a silent custom-route fallback).
    if enforce_contract:
        for hosted in hosted_agents:
            assert_adapter_contract(hosted)
        assert_agentos_contract()
        assert_agno_surface(composed, veridex_templates)

    # (6) OUTERMOST deny-by-default guard over http + websocket.
    return DenyByDefaultGuard(
        composed, veridex_matchers=veridex_matchers, settings=settings, verifier=verifier
    )


# The PINNED, reviewed agno==2.7.3 native surface (every entry is DENIED publicly by the guard). Kept
# as a module constant so an agno upgrade that ADDS a route trips the AC-29 "unclassified new route"
# check — forcing a human to re-review the deny-by-default policy rather than silently exposing it.
# Generated by composing AgentOS(agents=[adapter], db=InMemoryDb, base_app=veridex_app,
# on_route_conflict="preserve_base_app") and subtracting the veridex-owned routes.
_KNOWN_AGNO_NATIVE_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("DELETE", "/approvals/{approval_id}"),
        ("DELETE", "/components/{component_id}"),
        ("DELETE", "/components/{component_id}/configs/{version}"),
        ("DELETE", "/eval-runs"),
        ("DELETE", "/knowledge/content"),
        ("DELETE", "/knowledge/content/{content_id}"),
        ("DELETE", "/learnings/users/{user_id}"),
        ("DELETE", "/learnings/{learning_id}"),
        ("DELETE", "/memories"),
        ("DELETE", "/memories/{memory_id}"),
        ("DELETE", "/schedules/{schedule_id}"),
        ("DELETE", "/service-accounts/{service_account_id}"),
        ("DELETE", "/sessions"),
        ("DELETE", "/sessions/{session_id}"),
        ("GET", "/"),
        ("GET", "/agents"),
        ("GET", "/agents/{agent_id}"),
        ("GET", "/agents/{agent_id}/runs"),
        ("GET", "/agents/{agent_id}/runs/{run_id}"),
        ("GET", "/agents/{agent_id}/runs/{run_id}/checkpoints"),
        ("GET", "/agents/{agent_id}/runs/{run_id}/checkpoints/{message_index}"),
        ("GET", "/approvals"),
        ("GET", "/approvals/count"),
        ("GET", "/approvals/{approval_id}"),
        ("GET", "/approvals/{approval_id}/status"),
        ("GET", "/components"),
        ("GET", "/components/{component_id}"),
        ("GET", "/components/{component_id}/configs"),
        ("GET", "/components/{component_id}/configs/current"),
        ("GET", "/components/{component_id}/configs/{version}"),
        ("GET", "/config"),
        ("GET", "/eval-runs"),
        ("GET", "/eval-runs/{eval_run_id}"),
        ("GET", "/health"),
        ("GET", "/info"),
        ("GET", "/knowledge/config"),
        ("GET", "/knowledge/content"),
        ("GET", "/knowledge/content/{content_id}"),
        ("GET", "/knowledge/content/{content_id}/status"),
        ("GET", "/knowledge/{knowledge_id}/sources"),
        ("GET", "/knowledge/{knowledge_id}/sources/{source_id}/files"),
        ("GET", "/learnings"),
        ("GET", "/learnings/users"),
        ("GET", "/learnings/{learning_id}"),
        ("GET", "/memories"),
        ("GET", "/memories/{memory_id}"),
        ("GET", "/memory_topics"),
        ("GET", "/metrics"),
        ("GET", "/models"),
        ("GET", "/registry"),
        ("GET", "/schedules"),
        ("GET", "/schedules/{schedule_id}"),
        ("GET", "/schedules/{schedule_id}/runs"),
        ("GET", "/schedules/{schedule_id}/runs/{run_id}"),
        ("GET", "/service-accounts"),
        ("GET", "/sessions"),
        ("GET", "/sessions/{session_id}"),
        ("GET", "/sessions/{session_id}/runs"),
        ("GET", "/sessions/{session_id}/runs/{run_id}"),
        ("GET", "/teams"),
        ("GET", "/teams/{team_id}"),
        ("GET", "/teams/{team_id}/runs"),
        ("GET", "/teams/{team_id}/runs/{run_id}"),
        ("GET", "/teams/{team_id}/runs/{run_id}/checkpoints"),
        ("GET", "/teams/{team_id}/runs/{run_id}/checkpoints/{message_index}"),
        ("GET", "/trace_session_stats"),
        ("GET", "/traces"),
        ("GET", "/traces/filter-schema"),
        ("GET", "/traces/{trace_id}"),
        ("GET", "/user_memory_stats"),
        ("GET", "/workflows"),
        ("GET", "/workflows/{workflow_id}"),
        ("GET", "/workflows/{workflow_id}/runs"),
        ("GET", "/workflows/{workflow_id}/runs/{run_id}"),
        ("PATCH", "/components/{component_id}"),
        ("PATCH", "/components/{component_id}/configs/{version}"),
        ("PATCH", "/eval-runs/{eval_run_id}"),
        ("PATCH", "/knowledge/content/{content_id}"),
        ("PATCH", "/learnings/{learning_id}"),
        ("PATCH", "/memories/{memory_id}"),
        ("PATCH", "/schedules/{schedule_id}"),
        ("PATCH", "/sessions/{session_id}"),
        ("POST", "/agents/{agent_id}/runs"),
        ("POST", "/agents/{agent_id}/runs/{run_id}/cancel"),
        ("POST", "/agents/{agent_id}/runs/{run_id}/continue"),
        ("POST", "/agents/{agent_id}/runs/{run_id}/resume"),
        ("POST", "/agents/{agent_id}/sessions/{session_id}/fork"),
        ("POST", "/approvals/{approval_id}/resolve"),
        ("POST", "/components"),
        ("POST", "/components/{component_id}/configs"),
        ("POST", "/components/{component_id}/configs/{version}/set-current"),
        ("POST", "/databases/all/migrate"),
        ("POST", "/databases/{db_id}/migrate"),
        ("POST", "/eval-runs"),
        ("POST", "/knowledge/content"),
        ("POST", "/knowledge/remote-content"),
        ("POST", "/knowledge/search"),
        ("POST", "/learnings"),
        ("POST", "/memories"),
        ("POST", "/metrics/refresh"),
        ("POST", "/optimize-memories"),
        ("POST", "/schedules"),
        ("POST", "/schedules/{schedule_id}/disable"),
        ("POST", "/schedules/{schedule_id}/enable"),
        ("POST", "/schedules/{schedule_id}/trigger"),
        ("POST", "/service-accounts"),
        ("POST", "/sessions"),
        ("POST", "/sessions/{session_id}/rename"),
        ("POST", "/teams/{team_id}/runs"),
        ("POST", "/teams/{team_id}/runs/{run_id}/cancel"),
        ("POST", "/teams/{team_id}/runs/{run_id}/continue"),
        ("POST", "/teams/{team_id}/runs/{run_id}/resume"),
        ("POST", "/teams/{team_id}/sessions/{session_id}/fork"),
        ("POST", "/traces/search"),
        ("POST", "/workflows/{workflow_id}/runs"),
        ("POST", "/workflows/{workflow_id}/runs/{run_id}/cancel"),
        ("POST", "/workflows/{workflow_id}/runs/{run_id}/continue"),
        ("POST", "/workflows/{workflow_id}/runs/{run_id}/resume"),
        ("WEBSOCKET", "/workflows/ws"),
    }
)
