import { describe, it, expect } from 'vitest';
import { buildCompetitionConfig } from '@/lib/competition/config';

describe('buildCompetitionConfig', () => {
  it('carries pack_id + fixture_id as authoritative identity (not just market_scope text)', () => {
    const cfg = buildCompetitionConfig({
      competitionType: 'replay_arena',
      sourceMode: 'replay',
      executionMode: 'paper',
      marketScope: 'France v Morocco · 1X2',
      scoringWindow: null,
      rosterSize: 2,
      packId: 'curated',
      fixtureId: 18209181,
    });
    expect(cfg.pack_id).toBe('curated');
    expect(cfg.fixture_id).toBe(18209181);
    expect(cfg.market_scope).toBe('France v Morocco · 1X2');
    expect(cfg.roster_size).toBe(2);
  });

  it('omits pack_id/fixture_id when no catalog identity was selected', () => {
    const cfg = buildCompetitionConfig({
      competitionType: 'replay_arena',
      sourceMode: 'replay',
      executionMode: 'paper',
      marketScope: '',
      scoringWindow: null,
      rosterSize: 2,
      packId: null,
      fixtureId: null,
    });
    expect(cfg.pack_id).toBeUndefined();
    expect(cfg.fixture_id).toBeUndefined();
  });
});
