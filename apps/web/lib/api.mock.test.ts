import { describe, it, expect, vi, afterEach } from 'vitest';
import { getProofArtifact, getLeaderboard, getCockpitState, verifyProof, getInspectorRecord } from '@/lib/api';

afterEach(() => { vi.unstubAllEnvs(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('api MOCK MODE (frontend-only DEMO fixtures)', () => {
  it('defaults OFF — with the flag unset, readers FETCH the backend (mock NOT applied)', async () => {
    const fetchSpy = vi.fn(async () => new Response(JSON.stringify({ rows: [] }), { status: 200 }));
    vi.stubGlobal('fetch', fetchSpy);
    await getLeaderboard();
    expect(fetchSpy).toHaveBeenCalled(); // real backend path, not fixtures
  });

  describe('with NEXT_PUBLIC_VERIDEX_MOCK=1', () => {
    it('populates the fetch screens from the canonical wire fixtures WITHOUT touching the backend', async () => {
      vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
      const fetchSpy = vi.fn(() => { throw new Error('fetch must NOT be called in mock mode'); });
      vi.stubGlobal('fetch', fetchSpy);

      const proof = await getProofArtifact('any');
      expect(proof.evidence_hash).toBe('3f082b3504e0e5164c692ffd2e12265dabe3e1ce5b103a26499e371939c7d8d6');
      expect(proof.checks).toHaveLength(7);

      const rows = await getLeaderboard();
      expect(rows.map((r) => r.agent_id)).toContain('agent-alpha');

      const cockpit = await getCockpitState('any');
      expect(cockpit.competition_id).toBeTruthy();

      const verify = await verifyProof('any');
      expect(verify.checks).toHaveLength(7);

      const insp = await getInspectorRecord('any', 0);
      expect(insp.run_id).toBeTruthy();

      expect(fetchSpy).not.toHaveBeenCalled(); // every reader served from fixtures
    });

    it('NEVER labels mock data LIVE — source modes demote to replay/mixed (no fake-live, SEC doctrine)', async () => {
      vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
      const rows = await getLeaderboard();
      expect(rows.some((r) => r.source_mode === 'live')).toBe(false);
      expect((await getCockpitState('any')).header.source_mode).not.toBe('live');
      expect((await getProofArtifact('any')).source_mode).not.toBe('live');
    });
  });
});
