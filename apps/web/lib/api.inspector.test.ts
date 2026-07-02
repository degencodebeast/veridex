import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { getInspectorRecord } from '@/lib/api';
import type * as W from '@/lib/wire';

// Inspector doctrine quantities (Fair Value / Executable Edge / stake) are roadmap "Inspector
// enrichment" (B): the wire InspectorRecord carries none, so live shows honest "—" (null). Under mock
// they populate from a DEMO overlay. EDGE lives HERE (the Inspector), not on Markets.
const FIX = resolve(__dirname, '../../../contracts/fixtures');
const inspectorWire = JSON.parse(readFileSync(resolve(FIX, 'inspector_record.json'), 'utf8')) as W.InspectorRecord;

function stubFetch(impl: typeof fetch) { vi.stubGlobal('fetch', vi.fn(impl) as unknown as typeof fetch); }

beforeEach(() => { vi.restoreAllMocks(); });
afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

describe('inspector doctrine quantities (mock-gated demo, honest-"—" live)', () => {
  it('MOCK: Fair Value / Executable Edge / venue populate; Kelly (stake) stays UNSERVED (SEC-005)', async () => {
    vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
    const r = await getInspectorRecord('run_x', '0');
    expect(r.clv_explanation.fair_value_pct).not.toBeNull();
    expect(r.clv_explanation.executable_edge_bps).not.toBeNull(); // EDGE = a per-decision Inspector quantity
    expect(r.clv_explanation.venue_decimal_price).not.toBeNull();
    // Kelly/stake sizing is NEVER surfaced — not even in mock (SEC-005: sizing never rank/scoring/proof).
    expect(r.clv_explanation.stake_fraction).toBeNull();
    expect(typeof r.clv_explanation.clv_bps).toBe('number'); // the real scored value travels through
  });

  it('LIVE (mock off): the doctrine quantities are honest null ("—"), never a fabricated number', async () => {
    stubFetch(async () => new Response(JSON.stringify(inspectorWire), { status: 200 }));
    const r = await getInspectorRecord('run_x', '0');
    expect(r.clv_explanation.fair_value_pct).toBeNull();
    expect(r.clv_explanation.executable_edge_bps).toBeNull();
    expect(r.clv_explanation.stake_fraction).toBeNull();
  });
});
