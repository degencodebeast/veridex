"""II-5b — Level-3 OPERATOR-RUN acceptance harness (STUB; NOT YET EXECUTED).

STATUS: CLAIM WITHHELD. This is the non-executable Level-3 acceptance CHECKLIST/STUB the II-5b brief
requires. It is DELIBERATELY skipped: Level 3 is an OPERATOR-run, out-of-band sandbox acceptance that
needs (1) a real operator-created + reviewed Privy policy and key quorum, (2) real Privy credentials,
and (3) the concrete II-5c HTTP + request-authorization adapter — NONE of which exist at II-5b, which
is offline Levels 1-2 only. Until an operator runs this and banks a bounded, redacted acceptance
artifact, the honest state is **CODE_READY / CLAIM_WITHHELD**.

NO UI or judge copy may claim wallet provisioning "works" until this harness has been executed and its
evidence banked. This file is the enumerated operator procedure — not a passing test.

Run ONLY when an operator has provisioned the out-of-band resources and explicitly authorizes Level 3;
supply the real pinned policy/quorum ids + hashes + credentials through the secret manager (NEVER Git),
wire the concrete II-5c provider, then remove the skip and drive the steps below against the sandbox.
"""

from __future__ import annotations

import pytest

# The whole module is skipped: it is an operator-run acceptance harness, never part of the offline CI.
pytestmark = pytest.mark.skip(
    reason="Level-3 operator-run acceptance — requires a real operator-reviewed Privy policy/quorum, "
    "real credentials, and the II-5c HTTP adapter; NOT YET EXECUTED (CODE_READY / CLAIM_WITHHELD)."
)


# ---------------------------------------------------------------------------
# The operator checklist — the exact steps an operator MUST complete + record.
# ---------------------------------------------------------------------------

L3_OPERATOR_CHECKLIST: tuple[str, ...] = (
    "PRE-1  Out-of-band: operator CREATES + REVIEWS the Privy policy (default-deny, typed-data-only, "
    "        exactly the Order + ClobAuth primaryTypes) and the key quorum (real threshold + auth keys).",
    "PRE-2  Operator pins the exact policy_id, quorum_id, owner_id(==quorum_id), policy content hash, "
    "        full policy content hash, quorum content hash, and threshold via the secret manager "
    "        (NEVER Git; NEVER logs). Wire the concrete II-5c provider (real privy-authorization-signature).",
    "L3-1   Live policy/quorum resources MATCH the pinned ids AND hashes (coarse + full) AND threshold; "
    "        wallet.owner_id == the pinned+verified quorum id. Any drift → fail closed, STOP.",
    "L3-2   Exactly ONE wallet is created with the EXACT external_id, owner_id, and singleton policy_id.",
    "L3-3   Retry AND process-restart reconcile to the SAME wallet id + address (never a second mint); "
    "        an ambiguous (>1) external_id lookup is a custody incident → STOP, no pick/sort/mint.",
    "L3-4   The persisted provisioning record + ExecutionWalletBinding SURVIVE a restart and REVALIDATE "
    "        (binding_hash + provisioning_record_hash + full policy hash recompute identically).",
    "L3-5   ONE non-money typed-data signing PREFLIGHT succeeds and RECOVERS the provisioned address "
    "        (recovered signer == binding.wallet_address). This is the ONLY signing action.",
    "L3-6   NEGATIVE: NO order submission, NO CLOB mutation, NO live-money execution occurs; "
    "        live_guarded stays rejected; raw-hash signing / eth_sendTransaction remain refused.",
    "POST-1 Bank a BOUNDED, REDACTED acceptance artifact (non-secret ids/addresses/hashes/state only — "
    "        no bearer tokens, no signatures, no keys, no full response headers). Only then may copy "
    "        state that provisioning works.",
)


def l3_operator_acceptance_harness() -> None:
    """NON-EXECUTABLE operator harness stub — the ordered Level-3 acceptance steps.

    This intentionally raises: it is a STUB the operator implements against the real sandbox + the
    II-5c adapter, not an automated test. It exists so the exact acceptance contract is enumerated in
    code and discoverable, and so no path can silently treat Level 3 as done.
    """
    raise NotImplementedError(
        "Level-3 acceptance is operator-run and NOT YET EXECUTED. Implement against the real Privy "
        "sandbox + the II-5c adapter, then bank the redacted evidence. Steps:\n"
        + "\n".join(L3_OPERATOR_CHECKLIST)
    )


@pytest.mark.skip(reason="Level-3 operator-run acceptance — NOT YET EXECUTED (see module docstring).")
def test_l3_operator_acceptance_placeholder() -> None:
    """Placeholder so the L3 contract is visible in the test tree; skipped until an operator runs it."""
    l3_operator_acceptance_harness()
