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

  // fu-ii5: unlock ONLY the QuoteGuard/MM template for deploy (replay + dry-run). Arb/Spread — the
  // other heavy extension — STAYS locked. The per-template `deployable` flag is the carve-out; the
  // internal `complexity` stays 'heavy-extension' for both (only the card label/gate diverge).
  it('marks ONLY quoteguard_mm deployable; arb_spread (also heavy-extension) stays locked', () => {
    const byId = Object.fromEntries(STRATEGY_TEMPLATES.map((t) => [t.id, t]));
    expect(byId.quoteguard_mm.deployable).toBe(true);
    expect(byId.arb_spread.deployable ?? false).toBe(false);
    expect(STRATEGY_TEMPLATES.filter((t) => t.deployable).map((t) => t.id)).toEqual(['quoteguard_mm']);
    // deployable is layered over — not a replacement for — the internal complexity invariant.
    expect(byId.quoteguard_mm.complexity).toBe('heavy-extension');
  });
});
