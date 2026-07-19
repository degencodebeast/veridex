"""The production OFFLINE recording proposer — the default `dry_run` R4-A proposer for II-5.

II-5 deploys `quoteguard-mm` through `replay` / `replay_dry_run` ONLY (never `live` / `live_guarded`
— see `veridex.mm_strategy.session_factory`). This module supplies the default `FacadeDeps.proposer`
for that dry-run path: it records the typed `MMExecutionToolRequest` every attempted leg builds and
returns an HONEST `MMExecutionToolResult` (`execution_status="ABSTAINED"` — no order ever reached the
wire). It is a REAL production module (req 6: "promote the existing offline machinery, don't leave it
test-only"), not a test double — it touches NO signer, wallet, or venue wire primitive: its `adapter`
/ `signer` / `sources` parameters are received but NEVER used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from veridex.dust_execution.facade import MMExecutionToolRequest, MMExecutionToolResult

if TYPE_CHECKING:
    pass


@dataclass
class OfflineRecordingProposer:
    """OFFLINE dry-run proposer: records each request, returns an honest ABSTAINED result.

    Every call APPENDS the typed, pin-cross-checked request to :attr:`calls` (executable evidence of
    exactly the legs the decisions implied) and returns a result whose `execution_status` is always
    `"ABSTAINED"` — this proposer never claims an order reached the wire (Gate#3 MAJOR-3 honesty).
    `adapter` / `signer` / `sources` are accepted (the real facade signature requires them) but
    NEVER touched — no network, no signer, no socket.
    """

    calls: list[MMExecutionToolRequest] = field(default_factory=list)

    async def __call__(
        self,
        request: MMExecutionToolRequest,
        *,
        adapter: Any,
        signer: Any,
        sources: Any,
        **kwargs: Any,
    ) -> MMExecutionToolResult:
        self.calls.append(request)
        return MMExecutionToolResult(
            admission="APPROVED",
            reason_codes=(),
            execution_status="ABSTAINED",
            execution_reason_codes=("offline_dry_run",),
            lifecycle_receipt_ref=f"dust-lifecycle:offline:{request.session_id}:{len(self.calls)}",
            run_label="DUST_LIVE",
            calibration_label="UNCALIBRATED",
            edge_label="NOT_PROVEN_EDGE",
            evidence_class="EXPERIMENTAL_DUST",
            policy_hash=request.policy_hash,
        )
