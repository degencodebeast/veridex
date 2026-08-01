import { describe, it, expect, vi, afterEach } from 'vitest';
import { getCompetitions } from '@/lib/api';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('getCompetitions', () => {
  it('adapts GET /competitions, deriving a title from config.market_scope (no fabricated fields)', async () => {
    const wire = [
      { competition_id: 'c_1', status: 'running',
        config: { market_scope: 'France v Morocco · 1X2', source_mode: 'replay', execution_mode: 'paper', roster_size: 2 },
        run_id: null },
    ];
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(wire), { status: 200 })));
    const rows = await getCompetitions();
    expect(rows).toHaveLength(1);
    expect(rows[0].competitionId).toBe('c_1');
    expect(rows[0].status).toBe('running');
    expect(rows[0].title).toBe('France v Morocco · 1X2');
    expect(rows[0].sourceMode).toBe('replay');
    expect(rows[0].rosterSize).toBe(2);
  });

  it('falls back to the competition_id + null fields when config is absent (never invents a value)', async () => {
    const wire = [{ competition_id: 'c_2', status: 'created', config: {}, run_id: null }];
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(wire), { status: 200 })));
    const rows = await getCompetitions();
    expect(rows[0].title).toBe('c_2');
    expect(rows[0].rosterSize).toBeNull();
    // Absent source/exec are null — NOT fabricated 'replay'/'paper' (absent-value honesty, spec §7).
    expect(rows[0].sourceMode).toBeNull();
    expect(rows[0].executionMode).toBeNull();
  });
});
