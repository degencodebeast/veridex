import { describe, it, expect, vi, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { renderHook } from '@testing-library/react';
import { RUNTIME_OVERVIEW, RUNTIME_LOG } from '@/lib/fixtures/catalog';

// ============================================================================
// DEMO-PATH FIXTURE-PROHIBITION SCAN (T-2, scoped — anti-Potemkin core).
//
// COVERAGE (honest and explicit — this scan does NOT claim more than it locks):
//   ✅ POSITIVE LOCK — the genuinely BACKEND-WIRED judge-path surfaces (below) do NOT import or
//      fall back to catalog/maker ENTITY fixtures; with mock OFF their live path fetches, throws,
//      or honest-empties. A planted entity-fixture import on any of them FAILS this scan.
//   ✅ MOCK-GATED LOCK — `useAgentOps` (Agent Ops runtime) DOES import RUNTIME_* fixtures, but only
//      for its demo path; the runtime test proves mock-OFF honest-empties (isDemo=false, log=[]),
//      and the fixture is reachable ONLY under isMockEnabled().
//   ⚠️ NOT COVERED (documented gap, deferred to T-2b) — the browse screens in KNOWN_GAP_BROWSE_SCREENS
//      still render entity fixtures directly with mock OFF. This scan is TRANSPARENT about that gap
//      (it does not pretend those screens are clean); mock-gating them is T-2b, tracked below.
//
// The point is an HONEST scan that locks what is wired and names what is not — never a green that
// passes by pretending the Potemkin browse screens do not exist.
// ============================================================================

// Fabricated ENTITY-DATA fixtures (records rendered as if real). Banned on the wired judge path.
const BANNED_ENTITY_FIXTURES = new Set<string>([
  // lib/fixtures/catalog.ts
  'LEADERBOARD_ROWS', 'COMPETITIONS', 'AGENTS', 'AGENT_PROFILES', 'MY_AGENTS', 'MY_RUNS',
  'MY_REWARDS', 'ALERTS', 'ODDS_UPDATES', 'FIXTURES', 'FEED_HEALTH', 'RUNTIME_OVERVIEW', 'RUNTIME_LOG',
  // lib/fixtures/maker.ts
  'MAKER_ARENA_RESULT',
]);

// Legitimate presentational METADATA (role/caption labels) — NOT fabricated entity data, so NOT
// banned even though it lives under lib/fixtures/. (DEFAULT_POLICY_ENVELOPE was likewise relocated to
// lib/config/policy.ts in T-2 so it is out of the fixtures tree entirely.)
const ALLOWED_METADATA_FIXTURES = new Set<string>(['MAKER_AGENT_META']);

// The genuinely-wired judge-path surfaces this scan LOCKS (positive lock).
const WIRED_SURFACES: { label: string; file: string }[] = [
  { label: 'Studio deploy (screen)', file: 'components/screens/StudioScreen.tsx' },
  { label: 'Studio deploy (page)', file: 'app/(app)/studio/page.tsx' },
  { label: 'Instance detail (screen)', file: 'components/screens/InstanceScreen.tsx' },
  { label: 'Instance detail (page)', file: 'app/(app)/instances/[instanceId]/page.tsx' },
  { label: 'Agent Ops drawer', file: 'components/ops/AgentOpsDrawer.tsx' },
  { label: 'Cockpit (screen)', file: 'components/screens/cockpit/CockpitScreen.tsx' },
  { label: 'Cockpit stream hook', file: 'hooks/useArenaStream.ts' },
  { label: 'Cockpit CLV leaderboard', file: 'components/screens/cockpit/ClvLeaderboard.tsx' },
  { label: 'Cockpit (page)', file: 'app/(app)/arena/[competitionId]/page.tsx' },
  { label: 'Maker Proof Card (screen)', file: 'components/screens/proof/MakerProofCardScreen.tsx' },
  { label: 'Maker Proof Card (page)', file: 'app/(app)/proof/maker/[id]/page.tsx' },
  { label: 'QuoteGuard ablation (screen)', file: 'components/screens/proof/GuardAblationScreen.tsx' },
  { label: 'QuoteGuard ablation (page)', file: 'app/(app)/proof/maker-ablation/[instanceId]/page.tsx' },
];

// KNOWN, DOCUMENTED GAP — browse screens that still render entity fixtures directly with mock OFF.
// This scan does NOT cover these; each carries its owning follow-up. Tracked as T-2b (mock-gate to
// honest-empty). Listing them here keeps the gap visible and un-droppable.
const KNOWN_GAP_BROWSE_SCREENS: { screen: string; route: string; fixtures: string[]; owner: string }[] = [
  { screen: 'OperatorDashboardScreen', route: '/dashboard', fixtures: ['MY_RUNS', 'COMPETITIONS', 'ALERTS'], owner: 'T-2b (unscoped)' },
  { screen: 'CompetitionsScreen', route: '/competitions', fixtures: ['COMPETITIONS', 'MY_REWARDS'], owner: 'T-2b (F-4 residual)' },
  { screen: 'AgentsScreen', route: '/agents', fixtures: ['AGENTS', 'MAKER_ARENA_RESULT'], owner: 'T-2b (F-9 maker table — frozen aux lane)' },
  { screen: 'MarketsScreen', route: '/markets', fixtures: ['FIXTURES', 'ODDS_UPDATES', 'FEED_HEALTH', 'LEADERBOARD_ROWS'], owner: 'T-2b (unscoped)' },
  { screen: 'LeaderboardScreen', route: '/leaderboard', fixtures: ['LEADERBOARD_ROWS', 'MAKER_ARENA_RESULT'], owner: 'T-2b (F-9 maker table — frozen aux lane)' },
  { screen: 'DuelScreen', route: '/duel', fixtures: ['AGENTS', 'MAKER_ARENA_RESULT'], owner: 'T-2b (F-9 maker table — frozen aux lane)' },
  { screen: 'AgentProfileScreen (agents/[agentId])', route: '/agents/:id', fixtures: ['AGENT_PROFILES'], owner: 'T-2b (unscoped)' },
  { screen: 'ClonePreviewScreen (clone)', route: '/clone', fixtures: ['AGENT_PROFILES'], owner: 'T-2b (unscoped)' },
];

const readSource = (rel: string) => readFileSync(join(process.cwd(), rel), 'utf8');

/**
 * Returns the BANNED entity fixtures a source statically imports from `@/lib/fixtures/*`.
 * Allowed metadata (e.g. MAKER_AGENT_META) and non-fixture modules (config/policy, lib/catalog types)
 * are ignored — only fabricated entity data on the wired judge path trips the scan.
 */
export function bannedFixtureImports(source: string): string[] {
  const found: string[] = [];
  // `[^{}]*` scopes the capture to a SINGLE import's specifier block — it must not span across
  // earlier import statements (a greedy `[\s\S]*?` would swallow prior imports and mangle the names).
  const importRe = /import\s+(?:type\s+)?\{([^{}]*)\}\s+from\s+['"](@\/lib\/fixtures\/[^'"]+)['"]/g;
  let m: RegExpExecArray | null;
  while ((m = importRe.exec(source)) !== null) {
    const names = m[1]
      .split(',')
      .map((s) => s.trim().split(/\s+as\s+/)[0].trim())
      .filter(Boolean);
    for (const n of names) {
      if (BANNED_ENTITY_FIXTURES.has(n) && !ALLOWED_METADATA_FIXTURES.has(n)) found.push(n);
    }
  }
  return found;
}

afterEach(() => vi.unstubAllEnvs());

describe('demo-path fixture-prohibition scan (T-2)', () => {
  // The scan's catching power — a permanent, planted-import-free proof it detects regressions.
  describe('scan catching power (self-test)', () => {
    it('flags a planted entity-fixture import', () => {
      const planted = "import { COMPETITIONS, MY_REWARDS } from '@/lib/fixtures/catalog';";
      expect(bannedFixtureImports(planted)).toEqual(expect.arrayContaining(['COMPETITIONS', 'MY_REWARDS']));
    });
    it('flags a planted MAKER_ARENA_RESULT import', () => {
      const planted = "import { MAKER_ARENA_RESULT, MAKER_AGENT_META } from '@/lib/fixtures/maker';";
      expect(bannedFixtureImports(planted)).toEqual(['MAKER_ARENA_RESULT']);
    });
    it('does NOT flag allowed metadata (MAKER_AGENT_META) — no false positive', () => {
      expect(bannedFixtureImports("import { MAKER_AGENT_META } from '@/lib/fixtures/maker';")).toEqual([]);
    });
    it('does NOT flag the relocated production default (config, not fixtures)', () => {
      expect(bannedFixtureImports("import { DEFAULT_POLICY_ENVELOPE } from '@/lib/config/policy';")).toEqual([]);
    });
  });

  // POSITIVE LOCK — wired surfaces are clean.
  describe('wired judge-path surfaces do not import entity fixtures', () => {
    it.each(WIRED_SURFACES)('$label ($file) imports no banned entity fixture', ({ file }) => {
      const violations = bannedFixtureImports(readSource(file));
      expect(violations, `${file} imports banned entity fixture(s): ${violations.join(', ')}`).toEqual([]);
    });
  });

  // MOCK-GATED LOCK — Agent Ops runtime honest-empties with mock OFF; fixture only under mock ON.
  describe('Agent Ops runtime (useAgentOps) is mock-gated, not a fixture fallback', () => {
    // useAgentOps IS listed as a wired surface via the drawer, but the hook itself imports RUNTIME_*
    // for its demo path — so it is locked at RUNTIME (below) rather than by the static import scan.
    it('mock OFF: live path honest-empties (isDemo=false, empty log — never the RUNTIME_LOG fixture)', async () => {
      vi.resetModules();
      vi.doMock('@/lib/api', () => ({
        getInstances: vi.fn(async () => []),
        getRuntimeEvents: vi.fn(async () => []),
      }));
      const { useRuntimeEvents } = await import('@/components/ops/useAgentOps');
      const { result, unmount } = renderHook(() => useRuntimeEvents('momentum_fr'));
      expect(result.current.isDemo).toBe(false);
      expect(result.current.log).toEqual([]);
      expect(result.current.overview).not.toEqual(RUNTIME_OVERVIEW.momentum_fr);
      unmount();
      vi.doUnmock('@/lib/api');
    });

    it('mock ON: serves the RUNTIME_* fixture as flagged demo (isDemo=true) — reachable ONLY here', async () => {
      vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
      vi.resetModules();
      const { useRuntimeEvents } = await import('@/components/ops/useAgentOps');
      const { result, unmount } = renderHook(() => useRuntimeEvents('momentum_fr'));
      expect(result.current.isDemo).toBe(true);
      expect(result.current.log).toEqual(RUNTIME_LOG);
      expect(result.current.overview).toEqual(RUNTIME_OVERVIEW.momentum_fr);
      unmount();
    });
  });

  // TRANSPARENT GAP — the browse-screen Potemkin surface this scan deliberately does NOT cover.
  describe('known browse-screen gap is documented (deferred to T-2b, never silently dropped)', () => {
    it('enumerates every known Potemkin browse screen with its fixtures and owning follow-up', () => {
      expect(KNOWN_GAP_BROWSE_SCREENS.length).toBeGreaterThanOrEqual(8);
      for (const gap of KNOWN_GAP_BROWSE_SCREENS) {
        expect(gap.fixtures.length, `${gap.screen} must name the fixtures it still renders`).toBeGreaterThan(0);
        expect(gap.owner, `${gap.screen} must carry an owning follow-up`).toMatch(/T-2b/);
      }
    });
    it('does not claim any gap screen as a wired-and-locked surface', () => {
      const wiredFiles = WIRED_SURFACES.map((s) => s.file.toLowerCase());
      for (const gap of KNOWN_GAP_BROWSE_SCREENS) {
        const base = gap.screen.split(' ')[0].toLowerCase();
        expect(wiredFiles.some((f) => f.includes(base))).toBe(false);
      }
    });
  });
});
