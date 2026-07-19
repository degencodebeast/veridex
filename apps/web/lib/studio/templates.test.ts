import { describe, it, expect } from 'vitest';
import { STRATEGY_TEMPLATES } from '@/lib/studio/templates';
import { ARCHETYPES } from '@/lib/catalog';

describe('strategy templates (doctrine patch)', () => {
  it('exposes the doctrine templates (+ F-1 det-Drift / LLM-Drift) with honest complexity labels', () => {
    expect(STRATEGY_TEMPLATES.map((t) => t.label)).toEqual([
      'Value-vs-Venue', 'Stale-Line', 'Momentum', 'Contrarian/Fade', 'Arb/Spread', 'QuoteGuard/MM',
      'det-Drift', 'LLM-Drift',
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
    // deployable is layered over — not a replacement for — the internal complexity invariant.
    expect(byId.quoteguard_mm.complexity).toBe('heavy-extension');
  });
});

// ── F-1: det-Drift standalone template + LLM-Drift arena-only contestant ───────
// Operator-authorized deadline reconciliation (2026-07-19): the deploy path (build_agent) accepts ONLY
// the strategy Literal baseline|momentum|momentum-sharp|cumulative-drift|llm. `cumulative-drift` is the
// real "det-Drift" detector → a standalone Studio deploy. `llm-drift` does NOT exist in the deploy path
// (it lives ONLY in veridex/runtime/arena_comparison.py), so LLM-Drift is a pinned fair-comparison ARENA
// contestant with NO Studio deploy — never the generic `llm` agent relabeled.
describe('F-1 · det-Drift deployable + LLM-Drift arena-only templates', () => {
  const byId = () => Object.fromEntries(STRATEGY_TEMPLATES.map((t) => [t.id, t]));

  it('adds a det-Drift template that is deployable on a DETERMINISTIC mode (not llm)', () => {
    const det = byId().det_drift;
    expect(det).toBeTruthy();
    expect(det.label).toBe('det-Drift');
    expect(det.deployable).toBe(true);
    expect(det.arenaOnly ?? false).toBe(false);
    // cumulative-drift is a DETERMINISTIC detector — its card must not default into LLM mode.
    expect(det.defaultMode).not.toBe('llm');
    // Honest copy: standalone Studio deployment available.
    expect(det.blurb).toMatch(/det-Drift: standalone Studio deployment available/i);
  });

  it('adds an ARENA-ONLY LLM-Drift template — never deployable from Studio', () => {
    const llm = byId().llm_drift;
    expect(llm).toBeTruthy();
    expect(llm.label).toBe('LLM-Drift');
    expect(llm.arenaOnly).toBe(true);
    expect(llm.deployable ?? false).toBe(false);
    // Honest copy: pinned fair-comparison arena contestant.
    expect(llm.blurb).toMatch(/LLM-Drift: pinned fair-comparison arena contestant/i);
  });

  it('deployable templates are EXACTLY {quoteguard_mm, det_drift}; arena-only is EXACTLY {llm_drift}', () => {
    expect(STRATEGY_TEMPLATES.filter((t) => t.deployable).map((t) => t.id).sort())
      .toEqual(['det_drift', 'quoteguard_mm']);
    expect(STRATEGY_TEMPLATES.filter((t) => t.arenaOnly).map((t) => t.id)).toEqual(['llm_drift']);
    // Mutually exclusive: a template is deploy-from-Studio OR arena-only, never both.
    expect(STRATEGY_TEMPLATES.filter((t) => t.deployable && t.arenaOnly)).toEqual([]);
  });

  it('every template (incl. the new two) still maps onto a real frozen archetype', () => {
    for (const t of STRATEGY_TEMPLATES) expect(ARCHETYPES).toContain(t.archetype);
  });
});
