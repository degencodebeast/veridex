import { describe, it, expect } from 'vitest';
import { STRATEGY_TEMPLATES } from '@/lib/studio/templates';
import { ARCHETYPES } from '@/lib/catalog';

describe('strategy templates (doctrine patch)', () => {
  it('exposes the six doctrine templates with honest complexity labels', () => {
    expect(STRATEGY_TEMPLATES.map((t) => t.label)).toEqual([
      'Value-vs-Venue', 'Stale-Line', 'Momentum', 'Contrarian/Fade', 'Arb/Spread', 'QuoteGuard/MM',
    ]);
    const heavy = STRATEGY_TEMPLATES.filter((t) => t.complexity === 'heavy-extension').map((t) => t.label);
    expect(heavy).toEqual(['Arb/Spread', 'QuoteGuard/MM']);
  });

  it('maps every template onto a real frozen archetype', () => {
    for (const t of STRATEGY_TEMPLATES) {
      expect(ARCHETYPES).toContain(t.archetype);
    }
  });
});
