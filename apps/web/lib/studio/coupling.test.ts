import { describe, it, expect } from 'vitest';
import { isLlmLocked, resolveMode, availableModes, LLM_LOCKED_ARCHETYPES } from '@/lib/studio/coupling';

describe('archetype -> mode coupling (AC-007 / SEC-009)', () => {
  it('locks the LLM mode for deterministic-in-code archetypes', () => {
    expect([...LLM_LOCKED_ARCHETYPES]).toEqual(['value_clv', 'baseline']);
    expect(isLlmLocked('value_clv')).toBe(true);
    expect(isLlmLocked('baseline')).toBe(true);
    expect(isLlmLocked('momentum')).toBe(false);
    expect(isLlmLocked('contrarian')).toBe(false);
    expect(isLlmLocked('stale_line')).toBe(false);
  });

  it('snaps LLM back to numeric when the archetype locks it (AC-007 snap-back)', () => {
    expect(resolveMode('value_clv', 'llm')).toBe('numeric');
    expect(resolveMode('baseline', 'llm')).toBe('numeric');
  });

  it('preserves a non-LLM mode under a locked archetype', () => {
    expect(resolveMode('value_clv', 'rule')).toBe('rule');
    expect(resolveMode('value_clv', 'numeric')).toBe('numeric');
  });

  it('allows LLM when the archetype unlocks it', () => {
    expect(resolveMode('momentum', 'llm')).toBe('llm');
  });

  it('reports the LLM segment as locked for a locked archetype and unlocked otherwise', () => {
    const locked = availableModes('value_clv');
    expect(locked.find((m) => m.mode === 'llm')?.locked).toBe(true);
    const unlocked = availableModes('momentum');
    expect(unlocked.find((m) => m.mode === 'llm')?.locked).toBe(false);
    expect(unlocked.map((m) => m.mode)).toEqual(['llm', 'numeric', 'rule']);
  });
});
