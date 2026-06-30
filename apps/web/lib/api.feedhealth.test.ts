import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { getFeedHealth, adaptFeedHealth } from '@/lib/api';
import type * as W from '@/lib/wire';

// WD-4 feed-health telemetry: read-only, NEVER scored. The adapter must carry the honesty
// signals (replay / not-live / real staleness) verbatim — never coerce a healthy/live look.
const FIX = resolve(__dirname, '../../../contracts/fixtures');
const feedWire = JSON.parse(readFileSync(resolve(FIX, 'feed_health.json'), 'utf8')) as W.FeedHealth;

function stubFetch(impl: typeof fetch) { vi.stubGlobal('fetch', vi.fn(impl) as unknown as typeof fetch); }
function calls() { return (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls; }

beforeEach(() => { vi.restoreAllMocks(); });
afterEach(() => { vi.unstubAllGlobals(); });

describe('feed health (WD-4 telemetry — honest, never scored)', () => {
  it('maps wire FeedHealth → view-model preserving the honesty signals (replay / not-live / staleness)', () => {
    const h = adaptFeedHealth(feedWire);
    expect(h.source_mode).toBe('replay');
    expect(h.ws_live).toBe(false);            // the fixture is not a live stream — never coerced true
    expect(h.connected).toBe(false);
    expect(h.txline_configured).toBe(false);  // demo feed (TxLINE not configured)
    expect(h.staleness_s).toBe(feedWire.staleness_s); // REAL staleness carried through
    expect(h.stale).toBe(feedWire.stale);
    expect(h.ticks_seen).toBe(feedWire.ticks_seen);
    expect(h.events_per_min).toBe(feedWire.events_per_min);
  });

  it('GETs /feed/health and never an alias', async () => {
    stubFetch(async () => new Response(JSON.stringify(feedWire), { status: 200 }));
    await getFeedHealth();
    expect(String(calls()[0][0])).toMatch(/\/feed\/health$/);
  });
});
