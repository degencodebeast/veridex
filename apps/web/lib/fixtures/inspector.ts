// DEMO overlay for the Inspector doctrine quantities (roadmap "Inspector enrichment", B). The wire
// InspectorRecord carries NONE of these, so live renders "—" (null). Under mock the reader overlays
// these demo values. EDGE (executable_edge_bps) lives HERE — the Inspector — NOT on Markets. clv_bps
// is the REAL scored value and is NOT overlaid (it travels through from the record).
//
// SEC-005: Kelly/stake sizing is NEVER surfaced — not even in mock. `stake_fraction` stays null ("—")
// so the UI can never imply the sizing is a rank/scoring/proof input.
//
// NOTE (future real-wiring, not a demo change): these quantities do NOT exist on the served
// InspectorRecord (run_id+seq axis). A real wiring is bigger than a field-add — Fair Value must be
// SURFACED/labeled from `market_state.markets` (raw de-vigged `stable_prob_bps`; the system has no
// standalone "fair value"), and Executable Edge / venue price live on the EXECUTION lane (POLICY_RESULT
// payload + ExecutionReceipt, keyed by execution_id) → a cross-axis JOIN. Until then: mock demo, live "—".
import type { ClvExplanation } from '@/lib/contracts';

export const INSPECTOR_DEMO_QUANTITIES: Omit<ClvExplanation, 'clv_bps'> = {
  fair_value_pct: 67.9,          // de-margined consensus fair probability at entry
  closing_fair_value_pct: 69.7,  // ... at close
  venue_decimal_price: 1.472,    // the actual venue decimal price
  executable_edge_bps: 22.0,     // EV at the venue price (NOT CLV) — Inspector-only
  stake_fraction: null,          // Kelly/policy sizing — UNSERVED even in mock (SEC-005)
  plain: 'Fair value 67.9% → closing 69.7%; executable edge +22.0 bps at venue 1.472.',
};
