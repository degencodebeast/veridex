import { describe, it, expect } from 'vitest';
import {
  oddsUpdatesPath, oddsStreamPath, decodePrice, reconstructClosing, buildFamilies, SPORT_CATALOG,
} from '@/lib/txline/client';
import type { OddsUpdate } from '@/lib/catalog';

const u = (p: Partial<OddsUpdate>): OddsUpdate => ({
  fixture_id: 18172280, message_id: 'm', ts: 0, in_running: false,
  market_family: '1X2_PARTICIPANT_RESULT', market_parameters: null,
  price_names: ['FRA', 'Draw', 'BRA'], prices: [1472, 3550, 6100],
  pct: ['67.935', '28.169', '16.393'], ...p,
});

describe('txline client (CON-040 / AC-010 / REQ-041/042)', () => {
  it('targets /odds/updates and /odds/stream, never /odds/snapshot', () => {
    expect(oddsUpdatesPath(18172280)).toBe('/odds/updates/18172280');
    expect(oddsStreamPath()).toBe('/odds/stream');
    expect(oddsUpdatesPath(1)).not.toContain('snapshot');
  });

  it('decodes integer prices to decimal odds (x1000)', () => {
    expect(decodePrice(1472)).toBeCloseTo(1.472, 3);
    expect(decodePrice(3121)).toBeCloseTo(3.121, 3);
  });

  it('reconstructs the closing line from the last pre-InRunning update (CON-040)', () => {
    const updates = [
      u({ ts: 1, prices: [1500, 3500, 6000] }),
      u({ ts: 2, prices: [1472, 3550, 6100] }), // last pre-match
      u({ ts: 3, in_running: true, prices: [1300, 3000, 9000] }), // ignored
    ];
    expect(reconstructClosing(updates, 'FRA', '1X2_PARTICIPANT_RESULT')).toBeCloseTo(1.472, 3);
  });

  it('returns null closing when no pre-InRunning update exists (renders pending/—)', () => {
    expect(reconstructClosing([u({ in_running: true })], 'FRA', '1X2_PARTICIPANT_RESULT')).toBeNull();
  });

  it('builds the three market families with decimal + implied %', () => {
    const families = buildFamilies([u({})]);
    const keys = families.map((f) => f.key);
    expect(keys).toContain('1X2_PARTICIPANT_RESULT');
    const x12 = families.find((f) => f.key === '1X2_PARTICIPANT_RESULT')!;
    expect(x12.rows[0].outcomes[0].decimal).toBeCloseTo(1.472, 3);
    expect(x12.rows[0].outcomes[0].impliedPct).toBe('67.935');
  });

  it('exposes ONLY decimal-odds fields per outcome — no point-spread/depth/per-bookmaker (REQ-042)', () => {
    const x12 = buildFamilies([u({})])[0];
    const outcome = x12.rows[0].outcomes[0];
    // Unsupported-field discipline: the free feed gives decimal odds + implied % + a
    // reconstructed closing line ONLY. We must never surface fabricated depth/spread/books.
    expect(Object.keys(outcome).sort()).toEqual(['closing', 'decimal', 'impliedPct', 'name']);
  });

  it('exposes Soccer active and US College FB/BB disabled (REQ-041)', () => {
    const soccer = SPORT_CATALOG.find((s) => s.id === 'soccer')!;
    expect(soccer.enabled).toBe(true);
    expect(soccer.competitions.some((c) => /world cup/i.test(c.label))).toBe(true);
    const disabled = SPORT_CATALOG.filter((s) => !s.enabled);
    expect(disabled.length).toBeGreaterThanOrEqual(2);
    disabled.forEach((s) => expect(s.disabledReason).toMatch(/free feed|coming soon/i));
  });
});
