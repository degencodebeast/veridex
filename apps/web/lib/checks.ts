// The frozen 7-member CheckId enum (spec §4.3, contracts/veridex_api.contract.ts).
// Ids are LOWERCASE snake_case to match the backend/fixtures exactly. UI label
// "Score Recomputed" maps to metrics_recomputed. CLV is a metric and is NOT in
// this vocabulary (SEC-001).
export const CHECK_IDS = [
  'evidence_integrity',
  'metrics_recomputed',
  'manifest_bound',
  'llm_boundary',
  'policy_obeyed',
  'receipt_separation',
  'anchor',
] as const;

export type CheckId = (typeof CHECK_IDS)[number];

export const CHECK_LABELS: Record<CheckId, string> = {
  evidence_integrity: 'Evidence Integrity',
  metrics_recomputed: 'Score Recomputed',
  manifest_bound: 'Manifest Bound',
  llm_boundary: 'LLM Boundary',
  policy_obeyed: 'Policy Obeyed',
  receipt_separation: 'Receipt Separation',
  anchor: 'On-Chain Anchor',
};

// Render order on the Proof Card (matches the V4 layout).
export const CHECK_ORDER: readonly CheckId[] = CHECK_IDS;
