import { describe, it, expect, vi, afterEach } from 'vitest';
import { getReplayPacks } from '@/lib/api';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const RESPONSE = {
  packs: [
    {
      pack_id: 'curated',
      content_hash: 'abc123',
      provenance: 'genuine-txline',
      is_genuine: true,
      fixtures: [18209181, 18213979],
      fixture_metadata: [
        { fixture_id: 18209181, home_team: 'France', away_team: 'Morocco', kickoff_ts: 1783627200, label_source: 'captured' },
        { fixture_id: 18213979, home_team: 'Norway', away_team: 'England', kickoff_ts: 1783803600, label_source: 'captured' },
      ],
    },
  ],
};

describe('getReplayPacks', () => {
  it('adapts the enriched /replay-packs envelope, preserving raw fixture ids', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(RESPONSE), { status: 200 })));
    const packs = await getReplayPacks();
    expect(packs).toHaveLength(1);
    expect(packs[0].packId).toBe('curated');
    expect(packs[0].provenance).toBe('genuine-txline');
    expect(packs[0].fixtures).toEqual([18209181, 18213979]);
    expect(packs[0].fixtureMetadata[0].home_team).toBe('France');
    expect(packs[0].fixtureMetadata[0].label_source).toBe('captured');
  });
});
