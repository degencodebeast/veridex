import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { CHECK_IDS } from '@/lib/checks';
import type {
  VerifyResult, ProofArtifact, LeaderboardResponse, FeedHealth,
  InspectorRecord, CompetitionStateResponse, RuntimeEventsResponse,
} from '@/lib/wire';

// The frozen contract fixtures live at the repo root (outside apps/web). Read +
// parse them at test time so the wire types can never drift from the backend.
const FIX = resolve(__dirname, '../../../contracts/fixtures');
function load<T>(name: string): T {
  return JSON.parse(readFileSync(resolve(FIX, name), 'utf8')) as T;
}

const STATUSES = ['pass', 'fail', 'pending', 'not_applicable'];

describe('wire contract binding — fixtures parse into lib/wire types', () => {
  it('verify_response.json → VerifyResult (7 lowercase checks, CLV only in metrics)', () => {
    const v = load<VerifyResult>('verify_response.json');
    expect(v.verified).toBe(true);
    expect(v.evidence_hash).toBe(v.recomputed_evidence_hash);
    const ids = Object.keys(v.checks);
    expect(ids.sort()).toEqual([...CHECK_IDS].sort());
    expect(ids).not.toContain('clv'); // SEC-001: CLV is never a check
    for (const id of CHECK_IDS) {
      expect(STATUSES).toContain(v.checks[id].result);
    }
    expect(v.metrics?.clv).toBe(92); // CLV lives in metrics
    expect(v.proof_card.checks.evidence_integrity.result).toBe('pass');
  });

  it('proof_artifact.json → ProofArtifact (Record<CheckId, CheckResult>, no CLV check)', () => {
    const p = load<ProofArtifact>('proof_artifact.json');
    const ids = Object.keys(p.checks);
    expect(ids.sort()).toEqual([...CHECK_IDS].sort());
    expect(ids).not.toContain('clv');
    expect(p.evidence.evidence_hash.length).toBeGreaterThan(0);
    expect(typeof p.evidence.run_event_count).toBe('number');
    expect(p.metrics?.clv).toBe(92);
  });

  it('leaderboard.json → LeaderboardResponse with the WD-7 confidence fields', () => {
    const lb = load<LeaderboardResponse>('leaderboard.json');
    expect(lb.rows.length).toBeGreaterThan(0);
    const r = lb.rows[0];
    expect(typeof r.valid_count).toBe('number');
    expect(typeof r.clv_confidence).toBe('string');
    expect(typeof r.low_sample).toBe('boolean');
    expect(r.rank).toBe(1);
  });

  it('feed_health.json → FeedHealth with the extended WD-4 staleness view', () => {
    const f = load<FeedHealth>('feed_health.json');
    expect(typeof f.txline_configured).toBe('boolean');
    expect(typeof f.connected).toBe('boolean');
    expect(typeof f.ticks_seen).toBe('number');
    expect('staleness_s' in f).toBe(true);
    expect(typeof f.stale).toBe('boolean');
  });

  it('inspector_record.json → InspectorRecord', () => {
    const i = load<InspectorRecord>('inspector_record.json');
    expect(typeof i.run_id).toBe('string');
    expect(i.clv_bps).toBeDefined();
    expect(i.untrusted_llm_metadata).toBeDefined();
  });

  it('runtime_events.json → RuntimeEventsResponse (.events wrapper)', () => {
    const r = load<RuntimeEventsResponse>('runtime_events.json');
    expect(Array.isArray(r.events)).toBe(true);
  });

  // competition_state.json top-level matches the contract, but its proof_card is
  // still the pre-SEC-001 legacy shape (clv-in-checks) the contract migration note
  // warns about. This assertion is a DRIFT TRIPWIRE: it flips to failure once the
  // fixture is regenerated post-WD-5b — then switch to a strict ProofArtifact parse.
  it('competition_state.json → top-level CompetitionStateResponse; proof_card legacy (drift tripwire)', () => {
    const c = load<CompetitionStateResponse>('competition_state.json');
    expect(typeof c.competition_id).toBe('string');
    expect(typeof c.latest_seq).toBe('number');
    expect(Array.isArray(c.roster)).toBe(true);
    const checks = (c.proof_card?.checks ?? {}) as Record<string, unknown>;
    expect(Object.keys(checks)).toContain('clv'); // KNOWN legacy drift
  });
});
