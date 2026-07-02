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
}

// Six doctrine templates mapped onto the frozen 5-archetype enum. Arb/Spread and
// QuoteGuard/MM are honestly labeled heavy extensions (Phase-3), not core archetypes.
export const STRATEGY_TEMPLATES: StrategyTemplate[] = [
  { id: 'value_vs_venue', label: 'Value-vs-Venue', archetype: 'value_clv', defaultMode: 'numeric', complexity: 'core', blurb: 'Flag value vs the de-vigged Stable-Price consensus when recomputed edge clears the threshold.' },
  { id: 'stale_line', label: 'Stale-Line', archetype: 'stale_line', defaultMode: 'numeric', complexity: 'standard', blurb: 'React to lagging lines before the consensus catches up; favors the real-time feed tier.' },
  { id: 'momentum', label: 'Momentum', archetype: 'momentum', defaultMode: 'llm', complexity: 'standard', blurb: 'Follow in-play momentum; LLM proposes, the law recomputes every action.' },
  { id: 'contrarian_fade', label: 'Contrarian/Fade', archetype: 'contrarian', defaultMode: 'llm', complexity: 'standard', blurb: 'Fade overreactions; LLM proposes FADE/WIDEN actions, scored on recomputed CLV.' },
  { id: 'arb_spread', label: 'Arb/Spread', archetype: 'value_clv', defaultMode: 'numeric', complexity: 'heavy-extension', blurb: 'Cross-market consistency (1X2 vs O/U). Heavy extension — multi-market wiring is Phase-3.' },
  { id: 'quoteguard_mm', label: 'QuoteGuard/MM', archetype: 'baseline', defaultMode: 'rule', complexity: 'heavy-extension', blurb: 'Market-making / quote-guard rules. Heavy extension — inventory + two-sided quoting is Phase-3.' },
];

export const COMPLEXITY_LABEL: Record<Complexity, string> = {
  core: 'core', standard: 'standard', 'heavy-extension': 'heavy extension (Phase-3)',
};
