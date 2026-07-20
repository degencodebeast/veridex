// Production policy-envelope defaults (config, NOT demo data).
//
// These envelopes are the REAL deploy-time policy the Studio/Create paths pin into a run manifest —
// they are shipped configuration, not a browsable demo entity like the catalog fixtures. They lived
// in `lib/fixtures/catalog.ts` for historical reasons; they are relocated here (T-2) so the
// demo-path fixture-prohibition scan can ban every `lib/fixtures/*` ENTITY fixture (MY_AGENTS,
// COMPETITIONS, AGENTS, LEADERBOARD_ROWS, MAKER_ARENA_RESULT, …) on the judge path WITHOUT banning a
// legitimate production default. Behavior is byte-identical to the pre-relocation definitions.
import type { PolicyEnvelope } from '@/lib/catalog';

export const DEFAULT_POLICY_ENVELOPE: PolicyEnvelope = {
  max_stake: 100, max_orders_per_run: 5, max_orders_per_session: 20, max_orders_per_day: 50,
  venue_allowlist: ['sxbet'], market_allowlist: ['1X2_PARTICIPANT_RESULT', 'OVERUNDER_PARTICIPANT_GOALS'],
  min_edge_bps: 8, max_slippage_bps: 25, max_price: 4.5, max_quote_age_s: 30,
  cooldown_s: 10, human_approval_threshold: 250, kill_switch: false,
};

// MM-specific deploy envelope for the QuoteGuard/MM (quoteguard-mm) family. It is DELIBERATELY
// SEPARATE from DEFAULT_POLICY_ENVELOPE (the directional sxbet / 1X2 identity, shared with the
// directional deploy path + its market chips) so neither path repurposes the other. The identity
// here is coherent with the REAL-DATA PMXT/TxLINE maker tape `pmxt-txline-mm-18209181-v1`:
//   - market_allowlist[0] is the REAL Polymarket HOME-win token the tape's order book quotes on
//     (`pmxt:18209181:home_win` == veridex.mm_strategy.pmxt_tape.TOKEN_ID). The backend pins
//     manifest.market = market_allowlist[0] (session_factory.build_maker_manifest), so an incoherent
//     allowlist[0] would yield no ATTEMPTED leg.
//   - venue is Polymarket (`poly`), the tape's book venue.
// Only market_allowlist / venue_allowlist / min_edge_bps / max_stake are read by the MM deploy
// branch; the remaining fields keep the envelope a well-formed PolicyEnvelope.
export const MM_POLICY_ENVELOPE: PolicyEnvelope = {
  max_stake: 5, max_orders_per_run: 3, max_orders_per_session: 10, max_orders_per_day: 20,
  venue_allowlist: ['poly'], market_allowlist: ['pmxt:18209181:home_win'],
  min_edge_bps: 10, max_slippage_bps: 100, max_price: 1000, max_quote_age_s: 60,
  cooldown_s: 0, human_approval_threshold: 6, kill_switch: false,
};
