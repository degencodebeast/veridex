import type { Archetype } from '@/lib/catalog';

export type StudioMode = 'llm' | 'numeric' | 'rule';

// value_clv and baseline are deterministic-in-code: the LLM mode is impossible.
export const LLM_LOCKED_ARCHETYPES: readonly Archetype[] = ['value_clv', 'baseline'];

export function isLlmLocked(a: Archetype): boolean {
  return LLM_LOCKED_ARCHETYPES.includes(a);
}

// AC-007: switching to a locked archetype while in LLM mode snaps back to numeric;
// contradictory combos are impossible.
export function resolveMode(a: Archetype, requested: StudioMode): StudioMode {
  if (requested === 'llm' && isLlmLocked(a)) return 'numeric';
  return requested;
}

export function availableModes(a: Archetype): { mode: StudioMode; locked: boolean }[] {
  const llmLocked = isLlmLocked(a);
  return [
    { mode: 'llm', locked: llmLocked },
    { mode: 'numeric', locked: false },
    { mode: 'rule', locked: false },
  ];
}

// The QuoteGuard/MM template id — the deploy discriminator for the MM family (decision 2). Driving
// the MM payload off the SELECTED TEMPLATE (not the archetype) is deliberate: quoteguard_mm reuses
// the `baseline` archetype, so mapping by archetype alone would hijack every manual baseline pick.
// Moved here from StudioScreen so a non-screen module can share the deploy predicate without importing
// a React component — spec §14 note 2.
export const MM_TEMPLATE_ID = 'quoteguard_mm';

// F-1: the det-Drift template id — the deploy discriminator for the real `cumulative-drift` detector.
// Like MM, the STRATEGY is driven off the selected TEMPLATE (not the archetype): det-Drift reuses the
// `momentum` archetype for mode coupling, so a manual momentum pick still deploys momentum-sharp while
// THIS template deploys cumulative-drift. (build_agent accepts `cumulative-drift`; config.py:183.)
export const DET_DRIFT_TEMPLATE_ID = 'det_drift';

// F-1: the honest, EXHAUSTIVE strategy resolution. The old `toStrategy` ended in a silent
// `return 'momentum-sharp'`, so value_clv / contrarian / stale_line in a deterministic mode all deployed
// an IDENTICAL momentum-sharp agent under three different card names — that is the dishonesty F-1 removes.
// This resolver is bounded by what the deploy path actually supports (build_agent's Literal
// baseline|momentum|momentum-sharp|cumulative-drift|llm, plus the separate quoteguard-mm MM seam) and has
// NO default fallthrough: an unmapped combo returns a typed `unsupported` verdict so the UI can disable
// deploy — a strategy the card does not name is NEVER emitted.
export type StrategyResolution =
  | { supported: true; strategy: string }
  | { supported: false; reason: string };

export function resolveStrategy(
  templateId: string | null, archetype: Archetype, mode: StudioMode,
): StrategyResolution {
  // Template-driven overrides — the deploy discriminator is the SELECTED template, not the archetype
  // (so a manual archetype pick never hijacks a template family; mirrors the MM carve-out).
  if (templateId === MM_TEMPLATE_ID) return { supported: true, strategy: 'quoteguard-mm' };
  if (templateId === DET_DRIFT_TEMPLATE_ID) return { supported: true, strategy: 'cumulative-drift' };
  // Directional path — an honest map onto the build_agent Literal:
  //   • any llm-capable archetype in LLM mode → the generic `llm` agent (value_clv/baseline are
  //     LLM-locked in coupling.ts, so this branch only ever sees momentum/contrarian/stale_line).
  //   • momentum (deterministic) → momentum-sharp; baseline (deterministic) → baseline.
  if (mode === 'llm') return { supported: true, strategy: 'llm' };
  if (archetype === 'momentum') return { supported: true, strategy: 'momentum-sharp' };
  if (archetype === 'baseline') return { supported: true, strategy: 'baseline' };
  // value_clv / contrarian / stale_line in a deterministic mode: NO distinct backend strategy exists.
  // Return unsupported (never a silent momentum-sharp) — the deploy affordance is disabled for these.
  return {
    supported: false,
    reason: `${archetype} has no deterministic backend strategy — switch to LLM mode (if available), or pick a supported template (Momentum, det-Drift, or QuoteGuard/MM)`,
  };
}
