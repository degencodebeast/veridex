import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { render } from '@testing-library/react';
import { LandingScreen } from '@/components/screens/LandingScreen';
import { NAV_SECTIONS } from '@/lib/nav';

// COPY-LINT (T-2 honesty core). Veridex executes DRY-RUN / paper on recorded replay — no live money,
// no real venue orders, no live on-chain execution (proofs anchor on devnet; settlement is design-ahead,
// Phase 2D). The public-facing copy must never claim otherwise. This lint bans live-money /
// venue-execution / on-chain-execution overclaims on the LANDING page AND every primary-nav route, so
// no judge-facing surface can drift back into an overclaim. It is a deliberate tripwire: honest copy
// must simply avoid these literal phrases (e.g. say "dry-run fills", not "venue fills").
const OVERCLAIMS: { pattern: RegExp; why: string }[] = [
  { pattern: /real orders?/i, why: 'execution is dry-run / paper — no live orders are ever placed' },
  { pattern: /venue fills?/i, why: 'no venue execution — receipts are dry-run, never real venue fills' },
  { pattern: /live on solana/i, why: 'nothing executes live on Solana; proofs anchor on devnet, settlement is design-ahead (Phase 2D)' },
  { pattern: /real execution/i, why: 'execution is dry-run (no live money) — never real execution' },
];

// Every primary-nav route → its screen source. The coverage test below asserts this map covers
// EXACTLY the live nav (a new nav route with no entry here fails — no judge-nav route survives uncovered).
const NAV_ROUTE_SCREEN: Record<string, string> = {
  '/competitions': 'components/screens/CompetitionsScreen.tsx',
  '/arena': 'components/screens/ArenaEmptyState.tsx',
  '/markets': 'components/screens/MarketsScreen.tsx',
  '/leaderboard': 'components/screens/LeaderboardScreen.tsx',
  '/agents': 'components/screens/AgentsScreen.tsx',
};
const LANDING_SOURCE = 'components/screens/LandingScreen.tsx';
const readSource = (rel: string) => readFileSync(join(process.cwd(), rel), 'utf8');

function stubMatchMedia(reduced: boolean) {
  window.matchMedia = vi.fn().mockImplementation((q: string) => ({
    matches: reduced && /reduce/.test(q), media: q, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(),
    removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
}
beforeEach(() => stubMatchMedia(false));

describe('Landing / nav copy-honesty (T-2 — no live-money / venue / on-chain-execution overclaims)', () => {
  it('the RENDERED landing page carries none of the banned overclaims', () => {
    render(<LandingScreen />);
    const text = document.body.textContent ?? '';
    for (const { pattern, why } of OVERCLAIMS) {
      expect(text, `Landing overclaims — ${why} (matched ${pattern})`).not.toMatch(pattern);
    }
  });

  it('every primary-nav route is covered by the copy-lint (no judge-nav route survives uncovered)', () => {
    const navHrefs = NAV_SECTIONS.map((s) => s.href).sort();
    expect(Object.keys(NAV_ROUTE_SCREEN).sort()).toEqual(navHrefs);
  });

  it.each([['LANDING', LANDING_SOURCE], ...Object.entries(NAV_ROUTE_SCREEN)])(
    '%s source carries none of the banned overclaims',
    (_route, sourcePath) => {
      const src = readSource(sourcePath);
      for (const { pattern, why } of OVERCLAIMS) {
        expect(src, `${sourcePath} overclaims — ${why} (matched ${pattern})`).not.toMatch(pattern);
      }
    },
  );
});
