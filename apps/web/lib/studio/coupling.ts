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
