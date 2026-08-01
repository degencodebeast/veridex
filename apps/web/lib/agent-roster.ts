// The SHARED AgentSummary → PublicAgentRow adapter. ONE mapping, consumed by BOTH the Agents roster
// demo path (agents/page.tsx `?mock=1`) AND the Duel surface (E4) — never re-implemented per-page.
//
// HONESTY: an AgentSummary with `avg_clv_bps == null` has no scored board rows, so its proof_state is
// the roster-local 'unscored' (NOT the AgentSummary's proof_mode, which would overstate a proof that
// no score backs). A scored agent carries its real proof_mode. The public identity is derived honestly:
// owner_public_label is the demo owner label and origin is 'unknown' (the demo AgentSummary carries no
// real owner/origin) — never fabricated as a real operator id.
import type { AgentSummary, PublicAgentRow } from '@/lib/catalog';

export function agentSummaryToPublicRow(a: AgentSummary): PublicAgentRow {
  return {
    public_agent_id: a.agent_id,
    display_name: a.agent_name,
    owner_public_label: 'demo',
    origin: 'unknown',
    // Unscored (no CLV aggregation) ⇒ the honest 'unscored' state, never the AgentSummary proof_mode.
    proof_state: a.avg_clv_bps == null ? 'unscored' : a.proof_mode,
    archetype: a.archetype,
    mode: a.mode,
    avg_clv_bps: a.avg_clv_bps,
    runs: a.runs,
    valid_pct: a.valid_pct,
  };
}
