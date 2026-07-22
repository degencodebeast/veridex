import { describe, it, expect } from 'vitest';
import { FIXTURES, ODDS_UPDATES } from '@/lib/fixtures/catalog';
import { STRATEGY_TEMPLATES } from '@/lib/studio/templates';

describe('correctness fixes (mock-only relabel + template copy)', () => {
  it('mock fixture 18172280 is coherently labeled NLD v MAR (the real 18172280 teams)', () => {
    const f = FIXTURES.find((x) => x.fixture_id === 18172280);
    expect(f).toBeTruthy();
    // EXACT (not merely "not FRA/BRA") so another wrong label cannot pass. 18172280 = Netherlands v
    // Morocco per scripts/txline_live/cp1/fixtures.json (event_slug fifwc-nld-mar-2026-06-29).
    expect([f!.participant1, f!.participant2]).toEqual(['NLD', 'MAR']);
  });

  it('fixture 18172280 odds outcome names are relabeled coherently (no FRA/BRA outcome leaks)', () => {
    const rows = ODDS_UPDATES[18172280] ?? [];
    const names = rows.flatMap((r) => r.price_names);
    expect(names).toContain('NLD');
    expect(names).toContain('MAR');
    expect(names).not.toContain('FRA');
    expect(names).not.toContain('BRA');
  });

  it('QuoteGuard/MM template copy drops the stale "canned MM-mechanism fixture" wording', () => {
    const qg = STRATEGY_TEMPLATES.find((t) => t.id === 'quoteguard_mm');
    expect(qg).toBeTruthy();
    expect(qg!.blurb).not.toMatch(/canned MM-mechanism fixture/i);
  });
});
