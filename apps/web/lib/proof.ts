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

export function deriveProofChain(
  artifact: { checks: CheckResult[]; evidence_hash: string; anchor: { tx_signature: string | null } },
  manifestHash: string,
): ProofChainStep[] {
  const c = artifact.checks;
  return [
    { id: 'evidence', label: 'Evidence', sub: 'sealed RunEvents', hash: artifact.evidence_hash || '—', status: statusOf(c, 'evidence_integrity') },
    { id: 'pre-score', label: 'Pre-Score', sub: 'raw prescore', hash: '—', status: statusOf(c, 'evidence_integrity') },
    { id: 'score', label: 'Score', sub: 'law recompute', hash: '—', status: statusOf(c, 'metrics_recomputed') },
    { id: 'manifest', label: 'Manifest', sub: 'config+policy', hash: manifestHash || '—', status: statusOf(c, 'manifest_bound') },
    { id: 'anchor', label: 'Anchor', sub: 'memo+txoracle', hash: artifact.anchor.tx_signature || '—', status: statusOf(c, 'anchor') },
  ];
}
