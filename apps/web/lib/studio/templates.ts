import type { Archetype } from '@/lib/catalog';
import type { StudioMode } from './coupling';

export type Complexity = 'core' | 'standard' | 'heavy-extension';

export interface StrategyTemplate {
  id: string;
  label: string;
  archetype: Archetype;
  defaultMode: StudioMode;
  complexity: Complexity;
  blurb: string;
  // fu-ii5: per-template deploy carve-out. A heavy-extension template stays LOCKED unless it is
  // explicitly `deployable` — this unlocks ONLY quoteguard_mm (replay + dry-run) through the MM
  // family, while the other heavy extension (Arb/Spread) remains Phase-3-locked. F-1 also marks the
  // det-Drift template `deployable` (it emits the real `cumulative-drift` strategy).
  deployable?: boolean;
  // F-1: an ARENA-ONLY contestant — presented with a "Use in Arena" affordance and NO Studio deploy.
  // Operator reconciliation (2026-07-19): `strategy:"llm-drift"` does NOT exist in the deploy path
  // (build_agent rejects it); llm-drift lives ONLY in veridex/runtime/arena_comparison.py. So LLM-Drift
  // is a pinned fair-comparison arena contestant — NEVER a Studio deploy, never the generic `llm` agent.
  arenaOnly?: boolean;
}

// Six doctrine templates mapped onto the frozen 5-archetype enum. Arb/Spread stays an honestly
// labeled heavy extension (Phase-3). QuoteGuard/MM is `deployable` — it routes through the frozen
// `quoteguard-mm` MM family in replay + dry-run (live-money execution stays disabled in the UI).
export const STRATEGY_TEMPLATES: StrategyTemplate[] = [
  { id: 'value_vs_venue', label: 'Value-vs-Venue', archetype: 'value_clv', defaultMode: 'numeric', complexity: 'core', blurb: 'Flag value vs the de-vigged Stable-Price consensus when recomputed edge clears the threshold.' },
  { id: 'stale_line', label: 'Stale-Line', archetype: 'stale_line', defaultMode: 'numeric', complexity: 'standard', blurb: 'React to lagging lines before the consensus catches up; favors the real-time feed tier.' },
  { id: 'momentum', label: 'Momentum', archetype: 'momentum', defaultMode: 'llm', complexity: 'standard', blurb: 'Follow in-play momentum; LLM proposes, the law recomputes every action.' },
  { id: 'contrarian_fade', label: 'Contrarian/Fade', archetype: 'contrarian', defaultMode: 'llm', complexity: 'standard', blurb: 'Fade overreactions; the LLM proposes FADE/WIDEN actions, the law recomputes every action.' },
  { id: 'arb_spread', label: 'Arb/Spread', archetype: 'value_clv', defaultMode: 'numeric', complexity: 'heavy-extension', blurb: 'Cross-market consistency (1X2 vs O/U). Heavy extension — multi-market wiring is Phase-3.' },
  { id: 'quoteguard_mm', label: 'QuoteGuard/MM', archetype: 'baseline', defaultMode: 'rule', complexity: 'heavy-extension', deployable: true, blurb: 'Market-making / quote-guard rules with inventory + two-sided quoting. Runs the quoteguard-mm family against a SIMULATED synthetic replay of a canned MM-mechanism fixture (not live TxLINE, not a real match) — dry-run only, live-money execution disabled.' },
  // F-1: det-Drift is the real `cumulative-drift` detector — a DETERMINISTIC trend/drift strategy on
  // the de-vigged consensus (no LLM). It reuses the `momentum` archetype for mode coupling but its
  // strategy is fixed by the template id (mirrors how quoteguard_mm reuses `baseline`), so a manual
  // momentum pick still deploys momentum-sharp while this card deploys cumulative-drift.
  { id: 'det_drift', label: 'det-Drift', archetype: 'momentum', defaultMode: 'numeric', complexity: 'standard', deployable: true, blurb: 'Deterministic cumulative-drift detector — trend/drift on the de-vigged consensus, no LLM in the loop. det-Drift: standalone Studio deployment available.' },
  // F-1: LLM-Drift is an ARENA-ONLY contestant (no Studio deploy). `strategy:"llm-drift"` is not in the
  // deploy path — it exists ONLY in the arena-comparison harness — so this card offers "Use in Arena"
  // and no Deploy. The det-vs-llm-drift head-to-head is the Arena competition, not two Studio deploys.
  { id: 'llm_drift', label: 'LLM-Drift', archetype: 'momentum', defaultMode: 'llm', complexity: 'standard', arenaOnly: true, blurb: 'LLM-Drift: pinned fair-comparison arena contestant — the head-to-head opponent for det-Drift in the Arena, never a Studio deploy (the generic llm agent is not LLM-Drift).' },
];

export const COMPLEXITY_LABEL: Record<Complexity, string> = {
  core: 'core', standard: 'standard', 'heavy-extension': 'heavy extension (Phase-3)',
};
