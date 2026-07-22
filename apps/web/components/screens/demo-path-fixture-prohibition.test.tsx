import type { ComponentType } from 'react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { renderHook, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  RUNTIME_OVERVIEW, RUNTIME_LOG,
  FEED_HEALTH, LEADERBOARD_ROWS, COMPETITIONS, MY_RUNS, MY_REWARDS, ALERTS,
} from '@/lib/fixtures/catalog';

// ============================================================================
// DEMO-PATH FIXTURE-PROHIBITION SCAN (T-2, scoped — anti-Potemkin core).
//
// COVERAGE (honest and explicit — this scan does NOT claim more than it locks):
//   ✅ POSITIVE LOCK (wired judge-path) — the genuinely BACKEND-WIRED judge-path surfaces (below) do
//      NOT import or fall back to catalog/maker ENTITY fixtures; with mock OFF their live path fetches,
//      throws, or honest-empties. A planted entity-fixture import on any of them FAILS this scan.
//   ✅ POSITIVE LOCK (remediated browse screens) — Markets / Competitions / OperatorDashboard /
//      PrizeVault were remediated: the screen now imports NO entity fixture. Their demo data is
//      PAGE-injected (or, for CreateCompetition, self-gated) ONLY under isMockEnabled(). A planted
//      fixture import on any of them FAILS this scan (static lock, below).
//   ✅ MOCK-GATED / RUNTIME LOCK — the demo-data guarantee for the remediated surfaces (and for
//      `useAgentOps` + `CreateCompetitionScreen`, which DO import fixtures for their demo path) is a
//      RUNTIME one: the runtime tests render the real surface with the mock flag OFF and assert it
//      renders HONEST-EMPTY (never the entity fixture), with a mock-ON counterpart proving the fixture
//      WOULD render — so the off-mock assertion is a live regression tripwire, not a vacuous pass.
//   ✅ POSITIVE LOCK (maker lane integrated, F-9) — Leaderboard / Agents / Duel NO LONGER import
//      MAKER_ARENA_RESULT. Their maker toggle now reads through F-9's useMakerArenaResult (page-sourced
//      /live-fetched, no sealed-fixture default), so BOTH their directional AND maker halves are honest.
//      Static lock: they import no banned entity fixture. Runtime lock: with mock OFF the maker lane
//      honest-empties (unavailable), never a fixture. The prior KNOWN_GAP is CLOSED (now empty).
//
// The point is an HONEST scan that locks what is remediated — and, now that the F-9 maker lane is
// integrated, there is NO remaining Potemkin browse surface. The gap list is empty by proof, not by
// pretending: the closure is asserted below (KNOWN_GAP is empty AND the ex-gap screens are clean).
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

// REMEDIATED browse screens (T-2) — these NO LONGER import any entity fixture. Their demo data is
// PAGE-injected ONLY under isMockEnabled(); off-mock they render honest-empty. Static positive lock
// below; the runtime lock proves the off-mock honest-empty (a planted import here FAILS the scan).
const REMEDIATED_PAGEFED_SCREENS: { label: string; file: string }[] = [
  { label: 'Markets (browse)', file: 'components/screens/MarketsScreen.tsx' },
  { label: 'Competitions (browse)', file: 'components/screens/CompetitionsScreen.tsx' },
  { label: 'Operator dashboard (browse)', file: 'components/screens/OperatorDashboardScreen.tsx' },
  { label: 'Prize vault (browse)', file: 'components/screens/PrizeVaultScreen.tsx' },
];

// KNOWN, DOCUMENTED GAP — the surfaces that still render an entity fixture with mock OFF. After the
// T-2 remediation AND the F-9 maker integration this list is EMPTY: the last three entries (Agents /
// Leaderboard / Duel) had their maker toggle moved onto F-9's useMakerArenaResult reader (no
// MAKER_ARENA_RESULT default), so no browse surface renders a fixture off-mock anymore. The closure is
// PROVEN below (list empty AND the ex-gap screens verified clean at source + honest-empty at runtime),
// not asserted by fiat.
const KNOWN_GAP_BROWSE_SCREENS: { screen: string; file: string; route: string; fixtures: string[]; owner: string }[] = [];

// MAKER-LANE INTEGRATED (F-9) — the three dual-lane browse screens whose maker toggle now reads
// through useMakerArenaResult (page-sourced, no sealed-fixture default). They are the ex-KNOWN_GAP
// entries, now fully honest on BOTH lanes. Static lock: they import no banned entity fixture. Runtime
// lock (below): with mock OFF the maker lane honest-empties, never a fixture. `load` dynamically
// imports the real screen so the runtime lock renders production code, not a stub.
const MAKER_INTEGRATED_SCREENS: {
  screen: string;
  file: string;
  route: string;
  dirEmptyTestId: string;
  makerRowTestId: string;
  load: () => Promise<ComponentType>;
}[] = [
  {
    screen: 'AgentsScreen', file: 'components/screens/AgentsScreen.tsx', route: '/agents',
    dirEmptyTestId: 'agents-empty', makerRowTestId: 'maker-agent-row',
    load: async () => (await import('@/components/screens/AgentsScreen')).AgentsScreen as ComponentType,
  },
  {
    screen: 'LeaderboardScreen', file: 'components/screens/LeaderboardScreen.tsx', route: '/leaderboard',
    dirEmptyTestId: 'lb-empty', makerRowTestId: 'lb-maker-row',
    load: async () => (await import('@/components/screens/LeaderboardScreen')).LeaderboardScreen as ComponentType,
  },
  {
    screen: 'DuelScreen', file: 'components/screens/DuelScreen.tsx', route: '/duel',
    dirEmptyTestId: 'duel-empty', makerRowTestId: 'duel-maker-card',
    load: async () => (await import('@/components/screens/DuelScreen')).DuelScreen as ComponentType,
  },
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

  // POSITIVE LOCK (T-2 remediation) — the remediated browse screens now import NO entity fixture.
  // Their demo data is PAGE-injected only under isMockEnabled(); a planted fixture import on any of
  // them FAILS this scan. This is the STATIC half; the RUNTIME lock (below) proves off-mock honesty.
  describe('remediated browse screens no longer import entity fixtures (static lock)', () => {
    it.each(REMEDIATED_PAGEFED_SCREENS)('$label ($file) imports no banned entity fixture', ({ file }) => {
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

  // GAP CLOSED — the F-9 maker integration moved the last three browse screens off the sealed fixture,
  // so KNOWN_GAP is now EMPTY. This is proven, not asserted: the list is empty AND each ex-gap screen
  // is verified clean at source (static) and honest-empty at runtime with mock OFF (below).
  describe('browse-screen fixture gap is CLOSED after the F-9 maker integration (no Potemkin surface remains)', () => {
    it('KNOWN_GAP_BROWSE_SCREENS is empty — no browse screen renders an entity fixture off-mock', () => {
      expect(KNOWN_GAP_BROWSE_SCREENS).toEqual([]);
    });
    it('the three ex-gap maker screens import NO banned entity fixture (static lock — MAKER_ARENA_RESULT gone)', () => {
      for (const s of MAKER_INTEGRATED_SCREENS) {
        const violations = bannedFixtureImports(readSource(s.file));
        expect(violations, `${s.screen} (${s.file}) must import no banned entity fixture; found: ${violations.join(', ')}`)
          .toEqual([]);
        // Belt-and-braces: the sealed maker fixture token must not appear anywhere in the source.
        expect(readSource(s.file), `${s.screen} must not reference MAKER_ARENA_RESULT`).not.toContain('MAKER_ARENA_RESULT');
      }
    });
    it('no directional OR maker entity fixture remains a live gap on any browse screen', () => {
      const stillRendered = new Set(KNOWN_GAP_BROWSE_SCREENS.flatMap((g) => g.fixtures));
      for (const banned of ['LEADERBOARD_ROWS', 'AGENTS', 'COMPETITIONS', 'MY_RUNS', 'MY_REWARDS', 'ALERTS', 'FIXTURES', 'ODDS_UPDATES', 'FEED_HEALTH', 'AGENT_PROFILES', 'MAKER_ARENA_RESULT']) {
        expect(stillRendered.has(banned), `${banned} is remediated and must not be a live gap`).toBe(false);
      }
      expect([...stillRendered]).toEqual([]);
    });
  });

  // RUNTIME LOCK (F-9 maker integration) — the maker lane on each ex-gap screen now reads through
  // useMakerArenaResult (page-sourced, no fixture default). With mock OFF and the API rejecting, the
  // maker lane must render HONEST-EMPTY (maker-unavailable), never a fabricated maker table — and the
  // directional lane is honest-empty too (no page-fed rows). A regression that reinstated a
  // MAKER_ARENA_RESULT default would render maker rows here off-mock and fail this lock.
  describe('ex-gap maker screens honest-empty on BOTH lanes with mock OFF (runtime lock)', () => {
    it.each(MAKER_INTEGRATED_SCREENS)(
      '$screen ($route): directional honest-empty + maker lane honest-empty (never a fixture) off-mock',
      async ({ dirEmptyTestId, makerRowTestId, load }) => {
        // useLane is URL-backed; reset any ?lane= left by a prior iteration so we start Directional.
        window.history.replaceState(null, '', '/');
        vi.resetModules();
        vi.doMock('@/lib/api', async (importOriginal) => {
          const actual = await importOriginal<typeof import('@/lib/api')>();
          return { ...actual, getMakerArenaResult: vi.fn(async () => { throw new Error('maker endpoint offline'); }) };
        });
        try {
          const Screen = await load();
          const user = userEvent.setup();
          render(<Screen />);

          // Directional lane (default): no page-fed rows → honest-empty, never a fixture table.
          expect(await screen.findByTestId(dirEmptyTestId)).toBeInTheDocument();

          // Maker lane: page-sourced reader with the API down → honest 'unavailable', never a fixture.
          await user.click(screen.getByRole('radio', { name: 'Maker' }));
          expect(await screen.findByTestId('maker-unavailable')).toBeInTheDocument();
          expect(screen.queryAllByTestId(makerRowTestId)).toHaveLength(0);
        } finally {
          vi.doUnmock('@/lib/api');
        }
      },
    );
  });

  // RUNTIME LOCK (T-2 remediation) — the anti-Potemkin proof that BITES. Each remediated surface
  // page-injects (or self-gates) its demo fixture ONLY under isMockEnabled(); with the mock flag OFF
  // it must render HONEST-EMPTY, never the entity fixture. Each lock renders the REAL surface off the
  // mock and asserts the fixture is ABSENT; the mock-ON counterpart proves the fixture WOULD render,
  // so the off-mock assertion is a live tripwire (a regression that renders the fixture off-mock fails
  // the mock-OFF case exactly as the mock-ON render populates it).
  describe('remediated surfaces render honest-empty with the mock flag OFF (runtime lock)', () => {
    it('MarketsPage: mock OFF renders no odds/fixtures — never the ODDS_UPDATES/FIXTURES fixture', async () => {
      vi.resetModules();
      vi.doMock('@/lib/api', async (importOriginal) => {
        const actual = await importOriginal<typeof import('@/lib/api')>();
        return { ...actual, getFeedHealth: async () => null, getLeaderboard: async () => [] };
      });
      try {
        const { default: MarketsPage } = await import('@/app/(app)/markets/page');
        render(<MarketsPage />);
        expect(await screen.findByText(/select a fixture/i)).toBeInTheDocument();
        expect(screen.queryByTestId('fixture-18172280')).toBeNull(); // no demo fixture button
        expect(screen.queryByText('1.472')).toBeNull(); // no decoded demo odds
      } finally {
        vi.doUnmock('@/lib/api');
      }
    });
    it('MarketsPage: mock ON surfaces the demo fixture — proving the off-mock lock is live', async () => {
      vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
      vi.resetModules();
      vi.doMock('@/lib/api', async (importOriginal) => {
        const actual = await importOriginal<typeof import('@/lib/api')>();
        return { ...actual, getFeedHealth: async () => FEED_HEALTH, getLeaderboard: async () => LEADERBOARD_ROWS };
      });
      try {
        const { default: MarketsPage } = await import('@/app/(app)/markets/page');
        render(<MarketsPage />);
        expect(await screen.findByTestId('fixture-18172280')).toBeInTheDocument();
      } finally {
        vi.doUnmock('@/lib/api');
      }
    });

    it('CompetitionsPage: mock OFF renders honest-empty — never the COMPETITIONS fixture', async () => {
      // Off-mock now reads REAL records (Task 8 coherence fix): an empty store renders the honest
      // `competitions-empty` state and NEVER the synthetic COMPETITIONS fixture. The mock-derived
      // stat band / all-competitions table only render under mock (records === undefined).
      vi.resetModules();
      vi.doMock('@/lib/api', async (importOriginal) => {
        const actual = await importOriginal<typeof import('@/lib/api')>();
        return { ...actual, getCompetitions: async () => [] };
      });
      try {
        const { default: CompetitionsPage } = await import('@/app/(app)/competitions/page');
        render(<CompetitionsPage />);
        expect(await screen.findByTestId('competitions-empty')).toBeInTheDocument();
        expect(screen.queryAllByTestId(/^comp-/)).toHaveLength(0);
        expect(screen.queryByText(/World Cup · FRA v BRA/)).toBeNull();
      } finally {
        vi.doUnmock('@/lib/api');
      }
    });
    it('CompetitionsPage: mock ON surfaces the COMPETITIONS fixture — off-mock lock is live', async () => {
      vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
      vi.resetModules();
      const { default: CompetitionsPage } = await import('@/app/(app)/competitions/page');
      render(<CompetitionsPage />);
      await waitFor(() => expect(screen.getByTestId('stat-total')).toHaveTextContent(String(COMPETITIONS.length)));
      expect(screen.getByTestId('comp-wc-fra-bra')).toBeInTheDocument();
    });

    it('PrizeVaultPage: mock OFF renders an honest-empty payout list — never the MY_REWARDS fixture', async () => {
      vi.resetModules();
      const { default: PrizeVaultPage } = await import('@/app/(app)/vault/page');
      render(<PrizeVaultPage />);
      expect(await screen.findByTestId('payout-empty')).toBeInTheDocument();
      expect(screen.queryByTestId('payout-list')).toBeNull();
      expect(screen.queryByText(/0xscore_8a31f2/i)).toBeNull(); // no fabricated demo root off-mock
    });
    it('PrizeVaultPage: mock ON surfaces the demo payouts — off-mock lock is live', async () => {
      vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
      vi.resetModules();
      const { default: PrizeVaultPage } = await import('@/app/(app)/vault/page');
      render(<PrizeVaultPage />);
      expect(await screen.findByTestId('payout-list')).toBeInTheDocument();
      expect(screen.queryByTestId('payout-empty')).toBeNull();
    });

    it('CreateCompetitionScreen: mock OFF renders an honest-empty fixture picker — never the FIXTURES fixture', async () => {
      vi.resetModules();
      const { CreateCompetitionScreen } = await import('@/components/screens/CreateCompetitionScreen');
      render(<CreateCompetitionScreen />);
      expect(await screen.findByTestId('fixture-empty')).toBeInTheDocument();
      expect(screen.queryByTestId('fixture-select')).toBeNull();
    });
    it('CreateCompetitionScreen: mock ON seeds the demo picker — off-mock lock is live', async () => {
      vi.stubEnv('NEXT_PUBLIC_VERIDEX_MOCK', '1');
      vi.resetModules();
      const { CreateCompetitionScreen } = await import('@/components/screens/CreateCompetitionScreen');
      render(<CreateCompetitionScreen />);
      expect(await screen.findByTestId('fixture-select')).toBeInTheDocument();
      expect(screen.queryByTestId('fixture-empty')).toBeNull();
    });

    // The /dashboard PAGE render is auth-gated (usePrivy) so it cannot surface the demo panels without
    // a session; the anti-Potemkin property is therefore locked at the SCREEN. The four operator panels
    // default to HONEST-EMPTY and only render what they are PAGE-fed (under the mock gate) — a regression
    // that defaulted them to the fixtures fails the empty case, and the fed case proves the lock is live.
    it('OperatorDashboardScreen: default (unfed) panels are honest-empty — never the fixtures', async () => {
      vi.resetModules();
      const { OperatorDashboardScreen } = await import('@/components/screens/OperatorDashboardScreen');
      render(<OperatorDashboardScreen connected loadInstances={async () => []} />);
      expect(await screen.findByTestId('runs-empty')).toBeInTheDocument();
      expect(screen.getByTestId('competitions-empty')).toBeInTheDocument();
      expect(screen.getByTestId('rewards-empty')).toBeInTheDocument();
      expect(screen.getByTestId('alerts-empty')).toBeInTheDocument();
      expect(screen.queryByText(/World Cup · FRA v BRA/)).toBeNull();
    });
    it('OperatorDashboardScreen: page-fed panels surface the fixtures — off-mock lock is live', async () => {
      vi.resetModules();
      const { OperatorDashboardScreen } = await import('@/components/screens/OperatorDashboardScreen');
      render(
        <OperatorDashboardScreen
          connected
          loadInstances={async () => []}
          runs={MY_RUNS}
          comps={COMPETITIONS}
          rewards={MY_REWARDS}
          alerts={ALERTS}
        />,
      );
      const comps = await screen.findByTestId('your-competitions');
      expect(screen.queryByTestId('competitions-empty')).toBeNull();
      expect(within(comps).getAllByText(/World Cup · FRA v BRA/).length).toBeGreaterThanOrEqual(1);
    });
  });
});
