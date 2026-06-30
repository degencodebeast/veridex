import { describe, it, expect, vi } from 'vitest';

// Real teeth for the MOCK-MODE live→replay demotion (the honesty guard). The shipped fixtures
// are all `replay`, so a "no live in output" test would pass even if demote were deleted. Here
// we feed SYNTHETIC `source_mode: 'live'` fixtures through the readers and assert the output is
// `replay` — so removing the demote (helper OR the leaderboard inline) makes this go red.
vi.mock('@/lib/mock', async () => {
  const actual = await vi.importActual<typeof import('@/lib/mock')>('@/lib/mock');
  const f = actual.MOCK_FIXTURES;
  return {
    isMockEnabled: () => true,
    MOCK_FIXTURES: {
      ...f,
      leaderboard: { ...f.leaderboard, rows: f.leaderboard.rows.map((r) => ({ ...r, source_mode: 'live' })) },
      competition: { ...f.competition, config: { ...(f.competition.config as Record<string, unknown>), source_mode: 'live' } },
      proofArtifact: { ...f.proofArtifact, run: { ...(f.proofArtifact.run as Record<string, unknown>), source_mode: 'live' } },
    },
  };
});

import { getLeaderboard, getCockpitState, getProofArtifact, mockStatusSeed } from '@/lib/api';

describe('MOCK MODE live→replay demotion (synthetic-live teeth)', () => {
  it('demotes a synthetic LIVE source_mode to replay at every mock reader', async () => {
    // leaderboard inline demote
    const rows = await getLeaderboard();
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.every((r) => r.source_mode === 'replay')).toBe(true);
    expect(rows.some((r) => r.source_mode === 'live')).toBe(false);

    // cockpit header demote (via the demote() helper)
    expect((await getCockpitState('x')).header.source_mode).toBe('replay');

    // proof artifact demote (via the demote() helper)
    expect((await getProofArtifact('x')).source_mode).toBe('replay');
  });

  it('demotes the status-bar mock seed too — synthetic LIVE → REPLAY (the bar never shows fake LIVE)', () => {
    // mockStatusSeed feeds the shared status bar app-wide under mock; the real fixture is already
    // replay, so without a synthetic-live input this guard would be untested. This makes it bite.
    expect(mockStatusSeed()?.sourceMode).toBe('replay');
  });
});
