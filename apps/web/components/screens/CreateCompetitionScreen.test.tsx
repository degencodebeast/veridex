import { describe, it, expect, vi } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { CreateCompetitionScreen, type LaunchApi } from '@/components/screens/CreateCompetitionScreen';
import { MARKET_FAMILY_KEYS } from '@/lib/catalog';
import { GLOSSARY } from '@/lib/glossary';
import type { DeployedInstance } from '@/lib/api';

// ── test helpers ────────────────────────────────────────────────────────────────────────────────
function mkInstance(over: Partial<DeployedInstance> & { instance_id: string; agent_id: string }): DeployedInstance {
  return {
    template_id: 'value_clv', run_id: `run_${over.instance_id}`, status: 'sealed', source_mode: 'replay',
    execution_mode: 'paper', config_hash: 'a'.repeat(64), policy_hash: 'b'.repeat(64),
    operator_id: 'did:privy:op', runtime_handle: null, last_failure_reason: null,
    market_allowlist: ['moneyline'], venue_allowlist: ['polymarket'], created_at: '2026-07-17T00:00:00Z',
    ...over,
  };
}

const TWO_INSTANCES: DeployedInstance[] = [
  mkInstance({ instance_id: 'inst-a', agent_id: 'clv-hunter', template_id: 'deterministic' }),
  mkInstance({ instance_id: 'inst-b', agent_id: 'momentum-v3', template_id: 'momentum', source_mode: 'live' }),
];

function okApi(): LaunchApi {
  return {
    create: vi.fn().mockResolvedValue({ competition_id: 'c_1', status: 'draft' }),
    register: vi.fn().mockResolvedValue({ agent_id: 'a', config_hash: 'h', proof_mode: 'reproducible' }),
    start: vi.fn().mockResolvedValue({ competition_id: 'c_1', status: 'finalized', run_id: 'run_1' }),
  };
}

async function selectBothInstances(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByTestId('roster-inst-a'));
  await user.click(await screen.findByTestId('roster-inst-b'));
}

describe('CreateCompetitionScreen (REQ-015 / SEC-009)', () => {
  it('pins law/policy/proof/exec before entry and reflects the type choice', async () => {
    const user = userEvent.setup();
    render(<CreateCompetitionScreen />);
    const pinned = screen.getByTestId('pinned-config');
    expect(pinned).toHaveTextContent(/proof/i);
    const sourceGroup = screen.getByRole('radiogroup', { name: /source mode/i });
    await user.click(within(sourceGroup).getByRole('radio', { name: 'Replay' }));
    expect(pinned).toHaveTextContent(/reproducible/i); // replay -> reproducible proof mode
  });

  it('is honest that config is pinned pre-run and not live-editable mid-run (SEC-009)', () => {
    render(<CreateCompetitionScreen />);
    const pinned = screen.getByTestId('pinned-config');
    expect(pinned).toHaveTextContent(/frozen at entry/i);
    expect(pinned).toHaveTextContent(/new version/i); // changing after start = new version, never a mutate
  });

  it('final confirmation: scoring law, source, roster & execution mode are frozen for the run at start', () => {
    render(<CreateCompetitionScreen />);
    expect(screen.getByTestId('pinned-config')).toHaveTextContent(/frozen for the run at start/i);
  });
});

// ── F-4: roster + launch progression ─────────────────────────────────────────────────────────────
describe('CreateCompetitionScreen — roster (F-4 · owner-scoped, honest states)', () => {
  it('not signed in → prompts to connect wallet, never lists instances', () => {
    render(<CreateCompetitionScreen connected={false} loadInstances={vi.fn()} />);
    expect(screen.getByTestId('roster-auth')).toHaveTextContent(/connect wallet to list your instances/i);
    expect(screen.getByTestId('launch-auth')).toBeInTheDocument();
  });

  it('signed in, no eligible instances → "Deploy an agent in Studio first"', async () => {
    render(<CreateCompetitionScreen connected loadInstances={vi.fn().mockResolvedValue([])} />);
    expect(await screen.findByTestId('roster-no-eligible')).toHaveTextContent(/deploy an agent in studio first/i);
  });

  it('a pending/failed instance is NOT roster-eligible (no runnable pinned config)', async () => {
    const instances = [
      mkInstance({ instance_id: 'inst-ok', agent_id: 'ready-agent', status: 'sealed' }),
      mkInstance({ instance_id: 'inst-bad', agent_id: 'failed-agent', status: 'failed' }),
    ];
    render(<CreateCompetitionScreen connected loadInstances={vi.fn().mockResolvedValue(instances)} />);
    expect(await screen.findByTestId('roster-inst-ok')).toBeInTheDocument();
    expect(screen.queryByTestId('roster-inst-bad')).toBeNull();
  });

  it('a failed instance load renders an honest error, never a fixture fallback', async () => {
    render(<CreateCompetitionScreen connected loadInstances={vi.fn().mockRejectedValue(new Error('401'))} />);
    expect(await screen.findByTestId('roster-error')).toBeInTheDocument();
    expect(screen.queryByTestId('roster-inst-a')).toBeNull();
  });

  it('roster rows show ONLY real instance-record fields (identity · strategy · pinned config · source)', async () => {
    render(<CreateCompetitionScreen connected loadInstances={vi.fn().mockResolvedValue(TWO_INSTANCES)} />);
    const row = await screen.findByTestId('roster-inst-a');
    expect(row).toHaveTextContent('clv-hunter');
    expect(row).toHaveTextContent('deterministic');
    expect(row).toHaveTextContent(/inst inst-a/);
    expect(row).toHaveTextContent(/cfg:aaaaaa…aaaa/); // pinned config identity, shortHash
    expect(row).toHaveTextContent(/replay/);
  });

  it('selecting instances reflects the roster count in the pinned summary', async () => {
    const user = userEvent.setup();
    render(<CreateCompetitionScreen connected loadInstances={vi.fn().mockResolvedValue(TWO_INSTANCES)} />);
    await selectBothInstances(user);
    expect(screen.getByTestId('summary-roster')).toHaveTextContent(/2 agents/i);
  });
});

describe('CreateCompetitionScreen — launch progression (F-4 · create→register→start)', () => {
  it('launch is disabled until ≥2 eligible instances are selected', async () => {
    const user = userEvent.setup();
    render(<CreateCompetitionScreen connected loadInstances={vi.fn().mockResolvedValue(TWO_INSTANCES)} />);
    expect(screen.getByTestId('launch-button')).toBeDisabled();
    await user.click(await screen.findByTestId('roster-inst-a'));
    expect(screen.getByTestId('launch-button')).toBeDisabled(); // still only 1
    await user.click(screen.getByTestId('roster-inst-b'));
    expect(screen.getByTestId('launch-button')).toBeEnabled();
  });

  it('happy path: create → register each instance-bound entry → start → navigate to the arena', async () => {
    const user = userEvent.setup();
    const api = okApi();
    const onLaunched = vi.fn();
    render(<CreateCompetitionScreen connected loadInstances={vi.fn().mockResolvedValue(TWO_INSTANCES)} launchApi={api} onLaunched={onLaunched} />);
    await selectBothInstances(user);
    await user.click(screen.getByTestId('launch-button'));

    await waitFor(() => expect(screen.getByTestId('launch-started')).toBeInTheDocument());
    expect(api.create).toHaveBeenCalledTimes(1);
    expect(api.register).toHaveBeenCalledTimes(2);
    // each roster entry is INSTANCE-BOUND (instance_id + pinned config_hash), never fabricated
    expect(api.register).toHaveBeenCalledWith('c_1', expect.objectContaining({ instance_id: 'inst-a', config_hash: 'a'.repeat(64) }));
    expect(api.start).toHaveBeenCalledWith('c_1');
    expect(onLaunched).toHaveBeenCalledWith('c_1');
  });

  it('fires onCommit with EXACTLY the pinned config, and create receives it (SEC-009 commit-what-is-pinned)', async () => {
    const user = userEvent.setup();
    const api = okApi();
    const onCommit = vi.fn();
    render(<CreateCompetitionScreen connected loadInstances={vi.fn().mockResolvedValue(TWO_INSTANCES)} launchApi={api} onCommit={onCommit} />);
    // flip source to replay → proof becomes reproducible; the pinned value must travel with launch.
    const sourceGroup = screen.getByRole('radiogroup', { name: /source mode/i });
    await user.click(within(sourceGroup).getByRole('radio', { name: 'Replay' }));
    await selectBothInstances(user);
    await user.click(screen.getByTestId('launch-button'));

    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ source_mode: 'replay', proof_mode: 'reproducible' }));
    await waitFor(() => expect(api.create).toHaveBeenCalledWith(expect.objectContaining({ source_mode: 'replay', roster_size: 2 })));
  });

  it('replay-backed competition → create source_mode reads "replay", never "live" for a tape run', async () => {
    const user = userEvent.setup();
    const api = okApi();
    render(<CreateCompetitionScreen connected loadInstances={vi.fn().mockResolvedValue(TWO_INSTANCES)} launchApi={api} />);
    const sourceGroup = screen.getByRole('radiogroup', { name: /source mode/i });
    await user.click(within(sourceGroup).getByRole('radio', { name: 'Replay' }));
    await selectBothInstances(user);
    await user.click(screen.getByTestId('launch-button'));
    await waitFor(() => expect(api.create).toHaveBeenCalled());
    expect((api.create as ReturnType<typeof vi.fn>).mock.calls[0][0]).toMatchObject({ source_mode: 'replay' });
  });

  it('partial failure: one instance fails to register → surfaces retry + start-with-the-rest, no fabricated start', async () => {
    const user = userEvent.setup();
    const api = okApi();
    // inst-b fails; inst-a succeeds. Add a third so ≥2 register OK and start-with-rest is offered.
    const three = [...TWO_INSTANCES, mkInstance({ instance_id: 'inst-c', agent_id: 'sharp-fade' })];
    api.register = vi.fn().mockImplementation((_c: string, entry: { instance_id: string }) =>
      entry.instance_id === 'inst-b' ? Promise.reject(new Error('instance unreachable')) : Promise.resolve({ agent_id: 'x', config_hash: 'h', proof_mode: 'reproducible' }));
    render(<CreateCompetitionScreen connected loadInstances={vi.fn().mockResolvedValue(three)} launchApi={api} />);
    await user.click(await screen.findByTestId('roster-inst-a'));
    await user.click(screen.getByTestId('roster-inst-b'));
    await user.click(screen.getByTestId('roster-inst-c'));
    await user.click(screen.getByTestId('launch-button'));

    await waitFor(() => expect(screen.getByTestId('launch-partial')).toBeInTheDocument());
    expect(screen.getByTestId('launch-partial')).toHaveTextContent(/momentum-v3 failed to register/i);
    expect(api.start).not.toHaveBeenCalled(); // never auto-started past a partial failure
    expect(screen.getByTestId('launch-retry')).toBeInTheDocument();
    expect(screen.getByTestId('launch-start-rest')).toHaveTextContent(/start with 2/i);
  });

  it('retry re-registers ONLY the failed instance, then proceeds to start on success', async () => {
    const user = userEvent.setup();
    const api = okApi();
    let bAttempts = 0;
    api.register = vi.fn().mockImplementation((_c: string, entry: { instance_id: string }) => {
      if (entry.instance_id === 'inst-b') { bAttempts += 1; return bAttempts === 1 ? Promise.reject(new Error('unreachable')) : Promise.resolve({ agent_id: 'b', config_hash: 'h', proof_mode: 'reproducible' }); }
      return Promise.resolve({ agent_id: 'a', config_hash: 'h', proof_mode: 'reproducible' });
    });
    render(<CreateCompetitionScreen connected loadInstances={vi.fn().mockResolvedValue(TWO_INSTANCES)} launchApi={api} />);
    await selectBothInstances(user);
    await user.click(screen.getByTestId('launch-button'));
    await waitFor(() => expect(screen.getByTestId('launch-partial')).toBeInTheDocument());
    await user.click(screen.getByTestId('launch-retry'));
    await waitFor(() => expect(screen.getByTestId('launch-started')).toBeInTheDocument());
    expect(api.start).toHaveBeenCalledWith('c_1');
  });

  it('create failure → honest error, NO run started, nothing fabricated', async () => {
    const user = userEvent.setup();
    const api = okApi();
    api.create = vi.fn().mockRejectedValue(new Error('POST /competitions failed: 401'));
    render(<CreateCompetitionScreen connected loadInstances={vi.fn().mockResolvedValue(TWO_INSTANCES)} launchApi={api} />);
    await selectBothInstances(user);
    await user.click(screen.getByTestId('launch-button'));
    await waitFor(() => expect(screen.getByTestId('launch-error')).toBeInTheDocument());
    expect(api.register).not.toHaveBeenCalled();
    expect(api.start).not.toHaveBeenCalled();
  });
});

// ── V5 fidelity: type cards · market scope · SUMMARY sidebar (pinning honesty, unchanged) ─────────
describe('CreateCompetitionScreen V5 (wizard density · honest pins)', () => {
  it('renders the 4 REAL competition_type cards', () => {
    render(<CreateCompetitionScreen />);
    const cards = screen.getByTestId('type-cards');
    (['live_arena', 'replay_arena', 'head_to_head', 'prize_vault_challenge'] as const).forEach((t) =>
      expect(within(cards).getByTestId(`type-${t}`)).toBeInTheDocument());
  });

  it('selecting a type pins it into the launch config (SEC-009)', async () => {
    const user = userEvent.setup();
    const api = okApi();
    render(<CreateCompetitionScreen connected loadInstances={vi.fn().mockResolvedValue(TWO_INSTANCES)} launchApi={api} />);
    await user.click(within(screen.getByTestId('type-cards')).getByTestId('type-head_to_head'));
    await selectBothInstances(user);
    await user.click(screen.getByTestId('launch-button'));
    await waitFor(() => expect(api.create).toHaveBeenCalledWith(expect.objectContaining({ competition_type: 'head_to_head' })));
  });

  it('market-scope options are EXACTLY the real MARKET_FAMILY_KEYS — never invented markets', () => {
    render(<CreateCompetitionScreen />);
    const scope = screen.getByTestId('market-scope');
    MARKET_FAMILY_KEYS.forEach((k) => expect(within(scope).getByTestId(`market-${k}`)).toBeInTheDocument());
    expect(within(scope).queryByText(/BTTS|correct score|first goalscorer|half-time/i)).toBeNull();
  });

  it('composes market_scope from the selected fixture + real families; scoring window honest-empty when unset', async () => {
    const user = userEvent.setup();
    const api = okApi();
    render(<CreateCompetitionScreen connected loadInstances={vi.fn().mockResolvedValue(TWO_INSTANCES)} launchApi={api} />);
    const summary = screen.getByTestId('pinned-config');
    expect(within(summary).getByTestId('summary-market-scope')).toHaveTextContent(/FRA v BRA/i);
    expect(within(summary).getByTestId('summary-scoring-window')).toHaveTextContent(/full match/i);
    await selectBothInstances(user);
    await user.click(screen.getByTestId('launch-button'));
    await waitFor(() => expect(api.create).toHaveBeenCalledWith(expect.objectContaining({
      market_scope: expect.stringMatching(/FRA v BRA/i), scoring_window: null,
    })));
  });

  it('SUMMARY pins the real CompetitionConfig fields POST freezes; REPLAY source shows REPLAY, never LIVE', async () => {
    const user = userEvent.setup();
    render(<CreateCompetitionScreen />);
    const summary = screen.getByTestId('pinned-config');
    ['summary-type', 'summary-source', 'summary-exec', 'summary-market-scope', 'summary-scoring-window']
      .forEach((id) => expect(within(summary).getByTestId(id)).toBeInTheDocument());
    const sourceGroup = screen.getByRole('radiogroup', { name: /source mode/i });
    await user.click(within(sourceGroup).getByRole('radio', { name: 'Replay' }));
    expect(within(summary).getByTestId('summary-source')).toHaveTextContent(/replay/i);
    expect(within(summary).getByTestId('summary-source')).not.toHaveTextContent(/\blive\b/i);
  });

  it('renders NO fabricated law_hash / pin-hash — the create API surfaces none (row deferred, honest-absent)', () => {
    render(<CreateCompetitionScreen />);
    expect(screen.queryByText(/law_hash/i)).toBeNull();
    expect(screen.queryByText(/0x[0-9a-f]{6,}/i)).toBeNull();
  });

  it('pins "Config pinned ✓" with the frozen-at-create caption — NOT a hash digest', () => {
    render(<CreateCompetitionScreen />);
    const summary = screen.getByTestId('pinned-config');
    expect(within(summary).getByTestId('summary-config-pinned')).toHaveTextContent(/config pinned ✓/i);
    expect(within(summary).getByTestId('config-pinned-caption')).toHaveTextContent(/frozen at create.*Proof Card/i);
    expect(within(summary).getByTestId('summary-config-pinned')).not.toHaveTextContent(/0x[0-9a-f]{6,}/i);
  });

  it('InfoTip copy is single-sourced from lib/glossary.ts — no per-screen microcopy drift', () => {
    render(<CreateCompetitionScreen />);
    expect(screen.getByText(GLOSSARY.source_mode.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.execution_mode.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.proof_mode.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.config_pinned.definition)).toBeInTheDocument();
  });
});
