import { readFileSync, existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StudioScreen, buildDeployPayload } from '@/components/screens/StudioScreen';
import { PREFLIGHT_DISCLAIMER } from '@/lib/studio/preflight';
import { DEFAULT_POLICY_ENVELOPE, MM_POLICY_ENVELOPE } from '@/lib/fixtures/catalog';
import { GLOSSARY } from '@/lib/glossary';

// THE canonical Studio MM deploy payload — the ONE shared, committed contract fixture BOTH this
// frontend test and the backend E2E (tests/test_pmxt_txline_tape.py) consume, so the real UI payload
// and the backend it drives can NEVER drift. It is what `buildDeployPayload(...)` MUST emit for the
// QuoteGuard/MM template (Dry Run), and the exact body the backend resolves to a SEALED run with an
// ATTEMPTED-leg receipt through the PRODUCTION catalog.
// Resolve the repo-root shared fixture by walking up from cwd (vitest runs from apps/web) until the
// contracts/fixtures/ path exists — independent of the module URL scheme under vite's transform.
function readSharedFixture(rel: string): unknown {
  let dir = process.cwd();
  for (let i = 0; i < 8; i += 1) {
    const candidate = resolve(dir, rel);
    if (existsSync(candidate)) return JSON.parse(readFileSync(candidate, 'utf-8'));
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error(`shared fixture not found walking up from ${process.cwd()}: ${rel}`);
}
const CANONICAL_MM_DEPLOY_PAYLOAD = readSharedFixture(
  'contracts/fixtures/studio_mm_deploy_payload.json',
) as Record<string, unknown>;

// A resolved deploy response (the pinned instance + the async run_id). run_id is a REAL server hex
// handle (never a fabricated 0x-prefixed digest — the honesty doctrine still holds).
function okDeploy(body?: Record<string, unknown>) {
  return {
    ok: true,
    status: 200,
    json: async () => body ?? {
      instance_id: 'inst_demo',
      config_hash: 'a'.repeat(64),
      policy_hash: 'b'.repeat(64),
      run_id: 'run_deadbeefcafe',
    },
  } as unknown as Response;
}

// A fail-closed preflight 422 body that NAMES the failing check(s).
function failClosedDeploy(...failed: string[]) {
  return {
    ok: false,
    status: 422,
    json: async () => ({
      detail: {
        error: 'preflight_failed',
        failed_checks: failed,
        checks: failed.map((name) => ({ name, ok: false, detail: `${name} not ready` })),
      },
    }),
  } as unknown as Response;
}

describe('StudioScreen (REQ-018 / AC-007 / SEC-006/007/009)', () => {
  // The deploy button POSTs to /agents/deploy; stub fetch so unit tests never hit the network.
  // Default = a clean deploy; the fail-closed case overrides fetch in-test.
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async () => okDeploy()));
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });
  // ── Archetype cards + mode coupling (AC-007) ────────────────────────────────
  it('locks LLM mode for value_clv and prevents selecting it; momentum unlocks it (AC-007)', async () => {
    const user = userEvent.setup();
    render(<StudioScreen />);
    // default archetype is value_clv → LLM locked
    const llm = screen.getByRole('radio', { name: /LLM/ });
    expect(llm).toHaveAttribute('aria-disabled', 'true');
    await user.click(llm);
    expect(screen.getByRole('radio', { name: /numeric/i })).toHaveAttribute('aria-checked', 'true');
    // switch archetype to momentum → LLM unlocks
    await user.selectOptions(screen.getByLabelText(/archetype/i), 'momentum');
    expect(screen.getByRole('radio', { name: /LLM/ })).not.toHaveAttribute('aria-disabled', 'true');
  });

  it('snaps a selected LLM mode back to numeric when archetype switches to a locked one (AC-007 snap-back)', async () => {
    const user = userEvent.setup();
    render(<StudioScreen />);
    await user.selectOptions(screen.getByLabelText(/archetype/i), 'momentum');
    await user.click(screen.getByRole('radio', { name: /LLM/ }));
    expect(screen.getByRole('radio', { name: /LLM/ })).toHaveAttribute('aria-checked', 'true');
    await user.selectOptions(screen.getByLabelText(/archetype/i), 'value_clv');
    expect(screen.getByRole('radio', { name: /numeric/i })).toHaveAttribute('aria-checked', 'true');
  });

  it('keeps sections 02 and 03 mutually exclusive with continuous 01-05 numbering (AC-007)', async () => {
    const user = userEvent.setup();
    render(<StudioScreen />);
    // value_clv + numeric: section 03 active, 02 is "not applicable" stub
    expect(screen.getByTestId('section-03')).not.toHaveAttribute('data-inactive', 'true');
    expect(screen.getByTestId('section-02')).toHaveAttribute('data-inactive', 'true');
    expect(within(screen.getByTestId('section-02')).getByText(/not applicable in this mode/i)).toBeInTheDocument();
    // switch to momentum + LLM → section 02 active, 03 is a stub
    await user.selectOptions(screen.getByLabelText(/archetype/i), 'momentum');
    await user.click(screen.getByRole('radio', { name: /LLM/ }));
    expect(screen.getByTestId('section-02')).not.toHaveAttribute('data-inactive', 'true');
    expect(screen.getByTestId('section-03')).toHaveAttribute('data-inactive', 'true');
  });

  it('fences the LLM SportsActionTypes as NOT AN INPUT TO SCORE (SEC-007)', async () => {
    const user = userEvent.setup();
    render(<StudioScreen />);
    await user.selectOptions(screen.getByLabelText(/archetype/i), 'momentum');
    await user.click(screen.getByRole('radio', { name: /LLM/ }));
    const shell = screen.getByTestId('section-02');
    expect(within(shell).getByText(/NOT AN INPUT TO SCORE/i)).toBeInTheDocument();
    for (const t of ['WAIT', 'FLAG_VALUE', 'FOLLOW_MOMENTUM', 'FADE', 'WIDEN_OR_SUSPEND']) {
      expect(within(shell).getByText(t)).toBeInTheDocument();
    }
  });

  it('renders six strategy cards — only Arb/Spread stays Phase-3 locked; QuoteGuard/MM is now deployable (fu-ii5)', async () => {
    const user = userEvent.setup();
    render(<StudioScreen />);
    const gallery = screen.getByTestId('strategy-cards');
    for (const label of ['Value-vs-Venue', 'Stale-Line', 'Momentum', 'Contrarian/Fade', 'Arb/Spread', 'QuoteGuard/MM']) {
      expect(within(gallery).getByRole('button', { name: new RegExp(label) })).toBeInTheDocument();
    }
    // Only ONE Phase-3-locked card remains (Arb/Spread); the MM card no longer reads "Phase-3".
    expect(within(gallery).getAllByText(/heavy extension \(phase-3\)/i).length).toBe(1);
    const arb = within(gallery).getByRole('button', { name: /Arb\/Spread/ });
    const mm = within(gallery).getByRole('button', { name: /QuoteGuard\/MM/ });
    // Arb/Spread stays disabled and Phase-3-labeled.
    expect(arb).toBeDisabled();
    expect(arb).toHaveTextContent(/heavy extension \(phase-3\)/i);
    // HONEST LABEL: the now-deployable MM card must NOT imply it is Phase-3 locked.
    expect(mm).not.toBeDisabled();
    expect(mm).not.toHaveTextContent(/phase-3/i);
    // Clicking the MM card applies its (baseline) archetype — it is a real, selectable template now.
    await user.click(mm);
    expect(screen.getByLabelText(/archetype/i)).toHaveValue('baseline');
  });

  // ── fu-ii5: QuoteGuard/MM template deploys through the MM family ─────────────
  describe('QuoteGuard/MM template → quoteguard-mm family deploy (fu-ii5)', () => {
    async function selectMM(user: ReturnType<typeof userEvent.setup>) {
      await user.click(within(screen.getByTestId('strategy-cards')).getByRole('button', { name: /QuoteGuard\/MM/ }));
    }

    // ── frontend↔backend contract (Major 1): buildDeployPayload(MM) is the canonical payload ──
    // The real Studio click path emits EXACTLY the committed canonical fixture; the backend E2E drives
    // that SAME fixture through the production catalog. Pinning the whole object here means a change to
    // the emitted MM payload breaks this test AND is force-reflected into the backend (one source of
    // truth), so the click path and the backend can never silently drift (the parked defect: the UI
    // emitted `synthetic-mm-mechanism-v1` + sxbet/1X2, which the production catalog cannot resolve).
    it('buildDeployPayload(QuoteGuard/MM) emits EXACTLY the canonical shared MM deploy payload', () => {
      // Dry Run is the MM card default (the receipt-producing mode); the MM branch ignores the
      // archetype/mode/source args (it hardcodes replay + the MM envelope), so only exec is load-bearing.
      const payload = buildDeployPayload('baseline', 'rule', 'dry_run', 'replay', 'quoteguard_mm');
      expect(payload).toEqual(CANONICAL_MM_DEPLOY_PAYLOAD);
      // And it is the PMXT real-data tape key + coherent identity — never the unresolvable synthetic key.
      expect(payload.mm?.tape_ref).toBe('pmxt-txline-mm-18209181-v1');
      expect(payload.mm?.tape_ref).not.toBe('synthetic-mm-mechanism-v1');
      expect(payload.fixture_id).toBe(18209181);
      expect(payload.mm?.guard_enabled).toBe(true);
    });

    it('POSTs strategy:"quoteguard-mm" with a well-formed mm object and surfaces the run_id', async () => {
      const user = userEvent.setup();
      const fetchMock = vi.fn(async () => okDeploy());
      vi.stubGlobal('fetch', fetchMock);
      render(<StudioScreen />);

      await selectMM(user);
      await user.click(screen.getByRole('button', { name: /pin config & queue run/i }));

      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
      expect(url).toMatch(/\/agents\/deploy$/);
      expect(init.method).toBe('POST');
      const body = JSON.parse(init.body as string);
      // Dispatch discriminator + fail-closed-safe modes. Selecting the MM card defaults execution to
      // Dry Run (the only mode that yields a dry-run receipt / ATTEMPTED leg — replay+paper mints OPS
      // only), so the headline click path is receipt-producing by construction.
      expect(body.strategy).toBe('quoteguard-mm');
      expect(body.source_mode).toBe('replay');
      expect(body.execution_mode).toBe('dry_run');
      // The MM path uses the PMXT-coherent envelope (poly / the real home-win token), NOT the shared
      // directional envelope (sxbet / 1X2) — the tape's book quotes on market_allowlist[0].
      expect(body.market_allowlist).toEqual(MM_POLICY_ENVELOPE.market_allowlist);
      expect(body.market_allowlist).toEqual(['pmxt:18209181:home_win']);
      expect(body.venue_allowlist).toEqual(['poly']);
      expect(body.market_allowlist.length).toBeGreaterThan(0);
      // The MakerDeployConfig subset — the REAL-DATA tape catalog KEY the production catalog resolves.
      expect(body.mm).toBeTruthy();
      expect(body.mm.tape_ref).toBe('pmxt-txline-mm-18209181-v1'); // real recorded PMXT/TxLINE tape key
      expect(body.mm.guard_enabled).toBe(true);
      expect(body.mm.tif).toBe('GTC');
      expect(body.mm.max_orders_per_run).toBe(3);
      expect(body.mm.max_orders_per_session).toBe(10);
      expect(body.mm.max_orders_per_day).toBe(20);
      expect(body.mm.max_session_loss).toBe(0);
      expect(body.mm.max_daily_loss).toBe(0);

      expect(await screen.findByTestId('deploy-run-id')).toHaveTextContent('run_deadbeefcafe');
    });

    it('labels the MM source honestly as a SIMULATED REPLAY of REAL recorded data — never live/genuine/R3-sealed (A8)', async () => {
      const user = userEvent.setup();
      render(<StudioScreen />);
      const gallery = screen.getByTestId('strategy-cards');
      const mm = within(gallery).getByRole('button', { name: /QuoteGuard\/MM/ });
      // The card now reads as a SIMULATED REPLAY of REAL recorded in-play data (Polymarket + TxLINE,
      // France v Morocco), dry-run with live-money disabled — honest event branding is now CORRECT
      // because this is real recorded data, NOT a canned/synthetic fixture. It must NOT overclaim
      // ("genuine" / R3-sealed) and must NOT read "Phase-3" for the now-deployable card.
      expect(mm).toHaveTextContent(/simulated replay/i);
      expect(mm).toHaveTextContent(/real recorded/i);
      expect(mm).toHaveTextContent(/live-money (execution )?disabled/i);
      // No longer "synthetic": calling real recorded data synthetic would be an honesty regression.
      expect(mm).not.toHaveTextContent(/\bsynthetic\b/i);
      expect(mm).not.toHaveTextContent(/\bgenuine\b/i);
      expect(mm).not.toHaveTextContent(/phase-3/i);
      // The queued/deploy panel carries the same honest provenance once the MM template is selected.
      await user.click(mm);
      const note = screen.getByTestId('mm-provenance-note');
      expect(note).toHaveTextContent(/simulated .*replay/i);
      expect(note).toHaveTextContent(/real recorded/i);
    });

    it('exposes NO live source_mode / live_guarded execution affordance for the MM card (fail-closed by construction)', async () => {
      const user = userEvent.setup();
      render(<StudioScreen />);
      await selectMM(user);
      // Source: Replay only — no Live radio for the MM template.
      expect(screen.getByRole('radio', { name: /^Replay$/i })).toBeInTheDocument();
      expect(screen.queryByRole('radio', { name: /^Live$/i })).toBeNull();
      // Execution: paper / dry_run only — no Live Guarded.
      expect(screen.queryByRole('radio', { name: /Live Guarded/i })).toBeNull();
    });

    it('surfaces the fail-closed mm_family preflight failure (422) with no run_id', async () => {
      const user = userEvent.setup();
      vi.stubGlobal('fetch', vi.fn(async () => failClosedDeploy('mm_family')));
      render(<StudioScreen />);

      await selectMM(user);
      await user.click(screen.getByRole('button', { name: /pin config & queue run/i }));

      const err = await screen.findByTestId('deploy-preflight-error');
      expect(err).toHaveTextContent(/mm_family/);
      expect(screen.queryByTestId('deploy-run-id')).toBeNull();
    });
  });

  // ── fu-ii5: decision 2 must NOT hijack the directional deploy path ───────────
  describe('directional deploys stay directional (fu-ii5 regression guard)', () => {
    it('the default (value_clv) deploy carries NO mm object and a non-MM strategy', async () => {
      const user = userEvent.setup();
      const fetchMock = vi.fn(async () => okDeploy());
      vi.stubGlobal('fetch', fetchMock);
      render(<StudioScreen />);

      await user.click(screen.getByRole('button', { name: /pin config & queue run/i }));

      const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
      const body = JSON.parse(init.body as string);
      expect(body.mm).toBeUndefined();
      expect(body.strategy).not.toBe('quoteguard-mm');
    });

    it('a MANUALLY-picked baseline archetype still deploys directional "baseline" with NO mm (drives off template, not archetype)', async () => {
      const user = userEvent.setup();
      const fetchMock = vi.fn(async () => okDeploy());
      vi.stubGlobal('fetch', fetchMock);
      render(<StudioScreen />);

      // Pick baseline via the archetype select — NOT via the MM template card.
      await user.selectOptions(screen.getByLabelText(/archetype/i), 'baseline');
      await user.click(screen.getByRole('button', { name: /pin config & queue run/i }));

      const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
      const body = JSON.parse(init.body as string);
      expect(body.strategy).toBe('baseline'); // directional, NOT quoteguard-mm
      expect(body.mm).toBeUndefined();
    });
  });

  it('an edit produces a REVIEWABLE before→after diff, never a silent live mutation (#4 / SEC-009)', async () => {
    const user = userEvent.setup();
    render(<StudioScreen />);
    const diff = screen.getByTestId('config-diff');
    expect(within(diff).getByText(/no pending changes/i)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText(/archetype/i), 'momentum');
    const row = within(diff).getByTestId('diff-archetype');
    expect(row).toHaveTextContent(/value_clv/);
    expect(row).toHaveTextContent(/momentum/);
    expect(within(diff).getByText(/new (pinned )?version/i)).toBeInTheDocument();
  });

  it('is READ-ONLY during a scored run — no editable config affordances mid-run (SEC-006)', () => {
    render(<StudioScreen running />);
    expect(screen.getByLabelText(/archetype/i)).toBeDisabled();
    expect(screen.queryByRole('radio', { name: /numeric/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /pin config/i })).toBeNull();
    expect(screen.getByText(/read-only during a scored run/i)).toBeInTheDocument();
  });

  it('PIN CONFIG calls onPin and shows an honest "Config pinned ✓" affordance, then advances the baseline (SEC-009)', async () => {
    const user = userEvent.setup();
    const onPin = vi.fn();
    render(<StudioScreen onPin={onPin} />);
    await user.click(screen.getByRole('button', { name: /pin config & queue run/i }));
    expect(onPin).toHaveBeenCalledTimes(1);
    // The pin is represented as an honest affordance ("Config pinned ✓"), NOT a fabricated hash.
    expect(screen.getByTestId('config-pinned')).toHaveTextContent(/config pinned ✓/i);
    // After pin, baseline advances → no pending changes
    expect(within(screen.getByTestId('config-diff')).getByText(/no pending changes/i)).toBeInTheDocument();
    await screen.findByTestId('deploy-run-id'); // flush the async deploy state update
  });

  it('DOCTRINE: renders NO fabricated proof-flavored hash (no 0x… / config_hash / fiction hex) anywhere on Studio', async () => {
    const user = userEvent.setup();
    const { container } = render(<StudioScreen />);
    // Even after pinning, the pin is "Config pinned ✓" — never a fabricated 0x-prefixed digest.
    await user.click(screen.getByRole('button', { name: /pin config & queue run/i }));
    await screen.findByTestId('deploy-run-id'); // flush the async deploy; run_id is real, not 0x
    expect(container.textContent).not.toMatch(/0x[0-9a-z_]/i); // no 0xcfg_/0xpol_/0x… fiction hex
  });

  // ── T21: deploy button → real /agents/deploy endpoint ──────────────────────
  describe('deploy button → real endpoint (T21 / REQ-2D-701)', () => {
    it('POSTs the config to /agents/deploy and surfaces the returned run_id', async () => {
      const user = userEvent.setup();
      const fetchMock = vi.fn(async () => okDeploy());
      vi.stubGlobal('fetch', fetchMock);
      render(<StudioScreen />);

      await user.click(screen.getByRole('button', { name: /pin config & queue run/i }));

      // The real client POSTed to /agents/deploy (not loose client-only state).
      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
      expect(url).toMatch(/\/agents\/deploy$/);
      expect(init.method).toBe('POST');
      const body = JSON.parse(init.body as string);
      expect(body.execution_mode).toBe('paper');
      expect(body.market_allowlist).toEqual(DEFAULT_POLICY_ENVELOPE.market_allowlist);

      // The server-owned run_id is surfaced.
      expect(await screen.findByTestId('deploy-run-id')).toHaveTextContent('run_deadbeefcafe');
    });

    it('posts a demo-safe source_mode (replay, NOT hardcoded live) and surfaces the run_id (M6 honest default path)', async () => {
      // Codex M6: the Studio deploy must WORK from the default app. It hardcoded source_mode:'live',
      // which fails-closed on feed_health in the default app. The demo deploy defaults to a WORKING
      // REPLAY deploy (never dressed up as 'live'/live-money), so the headline flow is demonstrable.
      const user = userEvent.setup();
      const fetchMock = vi.fn(async () => okDeploy());
      vi.stubGlobal('fetch', fetchMock);
      render(<StudioScreen />);

      await user.click(screen.getByRole('button', { name: /pin config & queue run/i }));

      const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
      const body = JSON.parse(init.body as string);
      expect(body.source_mode).toBe('replay'); // demo-safe: NOT hardcoded 'live'
      expect(body.execution_mode).toBe('paper'); // paper: proof-only, never real money
      expect(await screen.findByTestId('deploy-run-id')).toHaveTextContent('run_deadbeefcafe');
    });

    it('surfaces the NAMED preflight failure fail-closed (422) and shows no run_id', async () => {
      const user = userEvent.setup();
      vi.stubGlobal('fetch', vi.fn(async () => failClosedDeploy('feed_health')));
      render(<StudioScreen />);

      await user.click(screen.getByRole('button', { name: /pin config & queue run/i }));

      const err = await screen.findByTestId('deploy-preflight-error');
      expect(err).toHaveTextContent(/feed_health/);
      expect(screen.queryByTestId('deploy-run-id')).toBeNull(); // no run started on preflight failure
    });
  });

  // ── PREFLIGHT PREVIEW — codex option 3 (TEETH) ─────────────────────────────
  // These tests are the RED-PROOF gate: adding a computed edge number or "Recomputed edge"
  // label MUST cause this suite to fail. All assertions below are intentionally strict.
  describe('preflight preview — threshold + rule-config + disclaimer ONLY (codex option 3)', () => {
    it('renders the REAL min-edge THRESHOLD as "Minimum executable edge ≥ N bps"', () => {
      render(<StudioScreen />);
      const preflight = screen.getByTestId('preflight');
      expect(within(preflight).getByTestId('threshold-row')).toBeInTheDocument();
      // The threshold label must be present
      expect(within(preflight).getByText(/Minimum executable edge/i)).toBeInTheDocument();
      // The threshold VALUE must include the real config bps (≥ 8 bps by default)
      expect(within(preflight).getByText(new RegExp(`≥\\s*${DEFAULT_POLICY_ENVELOPE.min_edge_bps}\\s*bps`))).toBeInTheDocument();
    });

    it('renders PREFLIGHT_DISCLAIMER verbatim (single-sourced from lib/studio/preflight)', () => {
      render(<StudioScreen />);
      const preflight = screen.getByTestId('preflight');
      expect(within(preflight).getByTestId('preflight-disclaimer')).toHaveTextContent(PREFLIGHT_DISCLAIMER);
    });

    it('renders the rule-config table with all policy envelope fields', () => {
      render(<StudioScreen />);
      const ruleConfig = screen.getByTestId('rule-config');
      expect(ruleConfig).toBeInTheDocument();
      expect(within(ruleConfig).getByText(/min_edge/i)).toBeInTheDocument();
      expect(within(ruleConfig).getByText(/max_slippage/i)).toBeInTheDocument();
      expect(within(ruleConfig).getByText(/kill_switch/i)).toBeInTheDocument();
      expect(within(ruleConfig).getByText(/venues/i)).toBeInTheDocument();
      expect(within(ruleConfig).getByText(/markets/i)).toBeInTheDocument();
    });

    // ── RED-PROOF TOOTH A: No "Recomputed edge" label ────────────────────────
    // If this label is added back (old pattern), this test MUST fail.
    it('TOOTH-A: "Recomputed edge" label MUST NOT appear in the preflight panel', () => {
      render(<StudioScreen />);
      const preflight = screen.getByTestId('preflight');
      // "recomputed edge" as a row label is forbidden — the word "recomputed" in the disclaimer
      // refers to run-time behavior, not a label for a computed value.
      expect(within(preflight).queryByText(/recomputed edge/i)).not.toBeInTheDocument();
    });

    // ── RED-PROOF TOOTH B: No computed edge value "+N.N bps" ─────────────────
    // The threshold shows "≥ 8 bps" (config, no "+"), never "+14.0 bps" (computed).
    it('TOOTH-B: no computed/estimated edge VALUE "+N.N bps" in the preflight panel', () => {
      render(<StudioScreen />);
      const preflight = screen.getByTestId('preflight');
      // A "+N.N bps" pattern (signed decimal) would indicate a computed edge estimate.
      expect(preflight.textContent).not.toMatch(/\+\d+\.?\d*\s*bps/);
    });

    // ── RED-PROOF TOOTH C: No ALLOW/DENY badge from a sample edge ────────────
    // The old preflight computed an ALLOW/DENY policyDecision from a hardcoded pre-run edge value —
    // that computed-edge pattern is forbidden (codex option 3); this tooth guards against it.
    it('TOOTH-C: no ALLOW/DENY badge computed from a sample edge in the preflight panel', () => {
      render(<StudioScreen />);
      const preflight = screen.getByTestId('preflight');
      expect(within(preflight).queryByText(/^ALLOW$/)).not.toBeInTheDocument();
      expect(within(preflight).queryByText(/^DENY$/)).not.toBeInTheDocument();
    });

    // ── RED-PROOF TOOTH D: No forbidden proof/scoring vocabulary ─────────────
    // CLV / verified / law result / proven / eligible / policy-approved must not appear as
    // labels or values within the preflight panel.
    // Note: "score" is intentionally excluded from the full-text check — the executable_edge
    // InfoTip definition ("...gates action; never a score.") legitimately uses the word in
    // a doctrine clarification. TOOTH-A + TOOTH-B already guard against the actual forbidden
    // patterns ("Recomputed edge" label and "+N.N bps" computed value).
    it('TOOTH-D: no CLV / verified / law result / proven / eligible / policy-approved in preflight content', () => {
      render(<StudioScreen />);
      const preflight = screen.getByTestId('preflight');
      const text = preflight.textContent ?? '';
      expect(text).not.toMatch(/\bCLV\b/);
      expect(text).not.toMatch(/\bverified\b/i);
      expect(text).not.toMatch(/\blaw result\b/i);
      expect(text).not.toMatch(/\bproven\b/i);
      expect(text).not.toMatch(/\beligible\b/i);
      expect(text).not.toMatch(/policy-approved/i);
    });
  });

  // ── InfoTip glossary drift-guard ─────────────────────────────────────────────
  // Each InfoTip MUST use verbatim text from GLOSSARY. If anyone inlines or paraphrases
  // the definition, these tests MUST fail (drift detected).
  describe('InfoTip glossary drift-guard', () => {
    function allTooltipTexts() {
      return screen.getAllByRole('tooltip').map((t) => t.textContent ?? '');
    }

    it('executable_edge InfoTip renders GLOSSARY.executable_edge.definition verbatim', () => {
      render(<StudioScreen />);
      expect(allTooltipTexts()).toContain(GLOSSARY.executable_edge.definition);
    });

    it('execution_mode InfoTip renders GLOSSARY.execution_mode.definition verbatim', () => {
      render(<StudioScreen />);
      expect(allTooltipTexts()).toContain(GLOSSARY.execution_mode.definition);
    });

    it('kelly InfoTip renders GLOSSARY.kelly.definition verbatim (policy sizing, never rank/scoring)', () => {
      render(<StudioScreen />);
      expect(allTooltipTexts()).toContain(GLOSSARY.kelly.definition);
    });

    it('source_mode InfoTip renders GLOSSARY.source_mode.definition verbatim', () => {
      render(<StudioScreen />);
      expect(allTooltipTexts()).toContain(GLOSSARY.source_mode.definition);
    });

    it('proof_mode InfoTip renders GLOSSARY.proof_mode.definition verbatim', () => {
      render(<StudioScreen />);
      expect(allTooltipTexts()).toContain(GLOSSARY.proof_mode.definition);
    });
  });
});
