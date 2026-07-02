// Studio PREFLIGHT PREVIEW builder (pure). The Studio screen (a catalog screen) calls this with the
// policy envelope it's configuring. FULLY (A) real config — no mock-gate.
//
// codex option 3: do NOT show a computed/estimated pre-run edge number — pre-run edge is a per-run
// sealed proof quantity (same risk class as Markets EDGE). Studio shows only the REAL min-edge
// THRESHOLD ("Minimum executable edge ≥ N bps") from the policy envelope + the rule-config table +
// the plain disclaimer below. NO proof styling; the preview must never claim to be recomputed/
// verified/proven/eligible or to carry a CLV/score/executable-edge VALUE.
import type { PolicyEnvelope, PreflightPreview, PreflightRule } from '@/lib/catalog';

// codex-pinned copy for the Studio preview (single-source; the screen renders this verbatim).
export const PREFLIGHT_DISCLAIMER =
  'Threshold config only. Actual edge is recomputed during the run from sealed evidence.';

export function buildPreflightPreview(policy: PolicyEnvelope): PreflightPreview {
  const rule_config: PreflightRule[] = [
    { field: 'min_edge', value: `${policy.min_edge_bps} bps` },
    { field: 'max_slippage', value: `${policy.max_slippage_bps} bps` },
    { field: 'max_price', value: String(policy.max_price) },
    { field: 'max_quote_age', value: `${policy.max_quote_age_s}s` },
    { field: 'max_stake', value: String(policy.max_stake) },
    { field: 'kill_switch', value: String(policy.kill_switch) },
    { field: 'venues', value: policy.venue_allowlist.join(', ') },
    { field: 'markets', value: policy.market_allowlist.join(', ') },
  ];
  return {
    policy_envelope: policy,                       // A: the real envelope pinned at create
    rule_config,                                    // A: derived from the real config
    min_edge_threshold_bps: policy.min_edge_bps,    // A: the real threshold — NOT a computed edge value
  };
}
