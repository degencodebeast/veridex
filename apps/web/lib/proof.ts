// Proof-card derivations. The wire ProofArtifact does NOT carry a chain[]; the
// chain STATUSES are derived from the 7 checks (REQ-020 gap decision). Hashes are
// shown only where we have them honestly: evidence_hash always; manifest_hash only
// after a verify; intermediate per-step hashes are not in the wire → "—" (never faked).
import type { CheckResult, ProofChainStep } from '@/lib/contracts';
import type { CheckStatus } from '@/components/ui/ProofCheckChip';
import type { CheckId } from '@/lib/checks';

function statusOf(checks: CheckResult[], id: CheckId): CheckStatus {
  return checks.find((c) => c.id === id)?.result ?? 'pending';
}

// The policy/exec step certifies the executor lane (policy envelope obeyed + receipt≠skill
// separation). Combine the two checks with no false-green: a fail dominates, then pending, then a
// pass needs at least one real check; when BOTH are not_applicable — Plan-A replay has no executor
// lane — the step is honestly not_applicable (never fabricated as pass).
function combinePolicyStatus(a: CheckStatus, b: CheckStatus): CheckStatus {
  if (a === 'fail' || b === 'fail') return 'fail';
  if (a === 'pending' || b === 'pending') return 'pending';
  if (a === 'pass' || b === 'pass') return 'pass';
  return 'not_applicable';
}

export function deriveProofChain(
  artifact: { checks: CheckResult[]; evidence_hash: string; anchor: { tx_signature: string | null } },
  manifestHash: string,
): ProofChainStep[] {
  const c = artifact.checks;
  return [
    { id: 'evidence', label: 'Evidence', sub: 'sealed RunEvents', hash: artifact.evidence_hash || '—', status: statusOf(c, 'evidence_integrity') },
    // pre-score has no dedicated check in the frozen 7 — the raw prescore is part of
    // the sealed evidence, so its trust status is evidence_integrity's (same hash chain).
    { id: 'pre-score', label: 'Pre-Score', sub: 'raw prescore', hash: '—', status: statusOf(c, 'evidence_integrity') },
    // The 6th step: policy/exec gate over the executor lane. No single wire hash (its policy/receipt
    // Merkle roots surface in the roots panel), so hash is honestly '—'.
    { id: 'policy', label: 'Policy', sub: 'exec envelope · receipt≠skill', hash: '—', status: combinePolicyStatus(statusOf(c, 'policy_obeyed'), statusOf(c, 'receipt_separation')) },
    { id: 'score', label: 'Score', sub: 'law recompute', hash: '—', status: statusOf(c, 'metrics_recomputed') },
    { id: 'manifest', label: 'Manifest', sub: 'config+policy', hash: manifestHash || '—', status: statusOf(c, 'manifest_bound') },
    { id: 'anchor', label: 'Anchor', sub: 'memo+txoracle', hash: artifact.anchor.tx_signature || '—', status: statusOf(c, 'anchor') },
  ];
}
