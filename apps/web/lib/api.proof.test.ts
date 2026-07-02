import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { getProofArtifact, adaptProofArtifact } from '@/lib/api';
import type * as W from '@/lib/wire';

// The backend root-forest (veridex/chain/merkle.py build_root_forest) produces EXACTLY 6 named
// roots — event_log/score/receipt/policy/competition/payout_reserved — into the manifest, but the
// served proof_card lineage does NOT include them yet. So: the adapter maps them when present (A,
// ready for when the backend serializes root_forest); under mock a demo forest populates; live is
// honest-empty until served. Names are REAL, never invented.
const FIX = resolve(__dirname, '../../../contracts/fixtures');
const proofWire = JSON.parse(readFileSync(resolve(FIX, 'proof_artifact.json'), 'utf8')) as W.ProofArtifact;
const EXPECTED = ['competition', 'event_log', 'payout_reserved', 'policy', 'receipt', 'score'];

function stubFetch(impl: typeof fetch) { vi.stubGlobal('fetch', vi.fn(impl) as unknown as typeof fetch); }

beforeEach(() => { vi.restoreAllMocks(); });
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

describe('proof merkle root-forest (6 real named roots, mock-gated population)', () => {
  // sha256(b"") — the backend EMPTY_ROOT (veridex/chain/merkle.py:16) for a domain with no records.
  const EMPTY_ROOT = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';

  it('MOCK: the 6 REAL roots — data domains carry demo hashes; no-executor-lane domains are honestly EMPTY_ROOT', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    const a = await getProofArtifact('run_x');
    expect(a.roots.map((r) => r.domain).sort()).toEqual(EXPECTED); // exactly the 6 real backend domains
    expect(a.roots.every((r) => /^[0-9a-f]{64}$/i.test(r.root))).toBe(true);
    expect(a.roots.every((r) => r.label.length > 0)).toBe(true);
    const root = (d: string) => a.roots.find((r) => r.domain === d)?.root;
    // demo REPLAY has no executor lane → these are honestly EMPTY_ROOT, NOT fabricated hashes
    expect(root('receipt')).toBe(EMPTY_ROOT);
    expect(root('policy')).toBe(EMPTY_ROOT);
    expect(root('payout_reserved')).toBe(EMPTY_ROOT);
    // domains with real replay data carry distinct demo hashes (never empty)
    expect(root('event_log')).not.toBe(EMPTY_ROOT);
    expect(root('score')).not.toBe(EMPTY_ROOT);
    expect(root('competition')).not.toBe(EMPTY_ROOT);
  });

  it('the adapter maps a REAL served root_forest when present (A, ready for backend)', () => {
    const withForest = { ...proofWire, lineage: { ...(proofWire.lineage as object), root_forest: {
      event_log: 'aa11', score: 'bb22', receipt: 'cc33', policy: 'dd44', competition: 'ee55', payout_reserved: 'ff66',
    } } } as W.ProofArtifact;
    const a = adaptProofArtifact(withForest);
    expect(a.roots.map((r) => r.domain).sort()).toEqual(EXPECTED);
    expect(a.roots.find((r) => r.domain === 'score')?.root).toBe('bb22'); // mapped from the REAL forest
  });

  it('LIVE (mock off): honest-empty roots until the backend serializes root_forest (never fabricated)', async () => {
    stubFetch(async () => new Response(JSON.stringify(proofWire), { status: 200 }));
    const a = await getProofArtifact('run_x');
    expect(a.roots).toEqual([]); // the served proof_card has no root_forest yet → honest-empty
  });
});
