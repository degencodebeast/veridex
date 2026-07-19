import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AgentOpsDrawer } from '@/components/ops/AgentOpsDrawer';
import type { CanonicalLogLine, RuntimeOverview } from '@/lib/catalog';
import type { RuntimeEventRecord } from '@/lib/api';

// F-6: the drawer's LIVE data path resolves the owner's instances and polls runtime-events. Both
// readers are mocked so the behaviour tests never touch the network; tests that assert specific data
// inject it via the drawer's props (which override the live hook), keeping them deterministic.
const getInstances = vi.fn();
const getRuntimeEvents = vi.fn();
const getInstanceStatus = vi.fn();
const killInstance = vi.fn();
const armCompetitionKillSwitch = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  getInstances: (...a: unknown[]) => getInstances(...a),
  getRuntimeEvents: (...a: unknown[]) => getRuntimeEvents(...a),
  getInstanceStatus: (...a: unknown[]) => getInstanceStatus(...a),
  killInstance: (...a: unknown[]) => killInstance(...a),
  armCompetitionKillSwitch: (...a: unknown[]) => armCompetitionKillSwitch(...a),
}));

// F-7 demo gate: lifecycle mutations are gated behind isMockEnabled at the hook (a kill is NEVER
// faked in demo). Default OFF so the behaviour tests exercise the LIVE control-plane path.
const isMockEnabled = vi.fn(() => false);
vi.mock('@/lib/mock', async (orig) => ({
  ...(await orig<typeof import('@/lib/mock')>()),
  isMockEnabled: () => isMockEnabled(),
}));

// F-7 lifecycle fixtures — one owned RUNNING instance for `momentum_fr`, resolvable to a running
// status. Individual tests override these to assert the disabled / error / terminal states.
const runningInstance = { instance_id: 'inst_1', agent_id: 'momentum_fr', run_id: 'run_1', status: 'running' };
const runningStatus = { instance_id: 'inst_1', run_id: 'run_1', run_state: 'running', killed: false, status: 'running', lease_status: 'active' };

const openState = (agentId: string) => ({ isOpen: true, agentId, open: vi.fn(), close: vi.fn() });

function overview(partial: Partial<RuntimeOverview> = {}): RuntimeOverview {
  return {
    agent_id: 'momentum_fr', run_id: 'run_1', status: 'running',
    latest_model_latency_ms: 412, latest_model_tokens: 318, last_action: 'FOLLOW_MOMENTUM',
    schema_valid: true, errors: 0, retries: 1, tool_calls: 3, source: 'STUDIO', ...partial,
  };
}
function ev(overrides: Partial<RuntimeEventRecord> = {}): RuntimeEventRecord {
  return {
    id: 1, type: 'action_emitted', agent_id: 'momentum_fr', run_id: 'run_live',
    session_id: 'sess_1', ts: 1782518393000, channel: 'OPS', payload: { action: 'FADE' }, ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  isMockEnabled.mockReturnValue(false);
  getInstances.mockResolvedValue([]); // default: owner has no matching instance → honest-empty
  getRuntimeEvents.mockResolvedValue([]);
  getInstanceStatus.mockResolvedValue(runningStatus);
  killInstance.mockResolvedValue({ instance_id: 'inst_1', run_id: 'run_1', phase: 'cancelling', engaged: true });
  armCompetitionKillSwitch.mockResolvedValue({ competition_id: 'comp_1', kill_switch: true, status: 'kill_switch_on' });
});
afterEach(() => { vi.restoreAllMocks(); });

describe('AgentOpsDrawer (REQ-030..032 / AC-003 / AC-030 / SEC-003 / F-6 live data)', () => {
  it('renders nothing when closed', () => {
    const { container } = render(
      <AgentOpsDrawer state={{ isOpen: false, agentId: null, open: vi.fn(), close: vi.fn() }} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('shows the read-only NOT SCORED fence header', () => {
    render(<AgentOpsDrawer state={openState('momentum_fr')} overviewByAgent={{ momentum_fr: overview() }} />);
    expect(screen.getByText(/RUNTIME OBSERVABILITY · READ-ONLY · NOT SCORED/i)).toBeInTheDocument();
  });

  it('#1 renders REAL runtime events from the owner-scoped endpoint (mock OFF) — the RUNTIME_LOG/RUNTIME_OVERVIEW fixtures are ABSENT from the live drawer', async () => {
    const user = userEvent.setup();
    getInstances.mockResolvedValue([{ instance_id: 'inst_1', agent_id: 'momentum_fr' }]);
    getRuntimeEvents.mockResolvedValue([
      ev({ id: 1, type: 'action_emitted', payload: { summary: 'LIVE-REAL-EVENT-XYZ' } }),
    ]);
    render(<AgentOpsDrawer state={openState('momentum_fr')} />); // NO props → live path
    await user.click(screen.getByRole('tab', { name: /logs/i }));
    await user.click(await screen.findByRole('radio', { name: /^all$/i })); // OPS is opt-in
    // REAL endpoint content renders…
    expect(await screen.findByText(/LIVE-REAL-EVENT-XYZ/i)).toBeInTheDocument();
    // …and the canned fixtures are GONE from the live path (RUNTIME_LOG / RUNTIME_OVERVIEW content).
    expect(screen.queryByText(/law_recomputed/i)).toBeNull();
    expect(screen.queryByText(/CLV \+14\.0 bps/i)).toBeNull();
    expect(screen.queryByText(/sxbet · paper · size 25/i)).toBeNull();
  });

  it('#5 mock OFF + no events → honest-empty drawer copy, NEVER a fixture', async () => {
    const user = userEvent.setup();
    render(<AgentOpsDrawer state={openState('momentum_fr')} />); // live path, getInstances → []
    // Overview: honest "no runtime data" (not the momentum_fr fixture overview)
    expect(await screen.findByText(/no runtime data/i)).toBeInTheDocument();
    expect(screen.queryByText(/FOLLOW_MOMENTUM/)).toBeNull(); // fixture last_action absent
    // Logs: honest "nothing yet", never the fixture log lines
    await user.click(screen.getByRole('tab', { name: /logs/i }));
    expect(screen.getByText(/no runtime events yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/law_recomputed/i)).toBeNull();
  });

  it('HIDES OPS telemetry by default (canonical-only) and only shows it on explicit opt-in to All (#1 / SEC-003)', async () => {
    const user = userEvent.setup();
    const OPS_LOG: CanonicalLogLine[] = [
      { ts: '14:03:13.917', channel: 'OPS', event: 'action_emitted', detail: 'FADE' },
      { ts: '14:03:14.118', channel: 'OPS', event: 'model_call_completed', detail: '412ms' },
    ];
    render(<AgentOpsDrawer state={openState('momentum_fr')} log={OPS_LOG} />);
    await user.click(screen.getByRole('tab', { name: /logs/i }));
    const log = screen.getByTestId('log');
    // default view is canonical → OPS runtime telemetry is ABSENT (all runtime events are OPS)
    expect(log.textContent).not.toMatch(/OPS/);
    // explicit opt-in to the telemetry (All) view → OPS appears
    await user.click(screen.getByRole('radio', { name: /^all$/i }));
    expect(screen.getByTestId('log').textContent).toMatch(/OPS/);
  });

  it('labels POLICY/EXEC derived · non-scoring in canonical view, offering no "proof-only" filter (AC-003)', async () => {
    const user = userEvent.setup();
    // POLICY/EXEC lines come from the competition event log (not runtime-events); the drawer still
    // renders their non-scoring label when such lines are supplied.
    const MERGED: CanonicalLogLine[] = [
      { ts: '14:03:13.941', channel: 'PROOF', event: 'law_recomputed', detail: 'valid' },
      { ts: '14:03:13.958', channel: 'POLICY', event: 'policy_result', detail: 'ALLOW' },
      { ts: '14:03:14.002', channel: 'EXEC', event: 'submitted', detail: 'paper' },
    ];
    render(<AgentOpsDrawer state={openState('momentum_fr')} log={MERGED} />);
    await user.click(screen.getByRole('tab', { name: /logs/i }));
    const log = screen.getByTestId('log');
    expect(within(log).getAllByText(/derived · non-scoring/i).length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByRole('radio', { name: /proof.only/i })).toBeNull();
  });

  it('shows OPS telemetry read-only + labeled non-scoring, with NO score/proof/CLV affordance (#2 / SEC-003)', async () => {
    const user = userEvent.setup();
    const OPS_ONLY: CanonicalLogLine[] = [
      { ts: '14:03:13.917', channel: 'OPS', event: 'action_emitted', detail: 'BACK FRA 1X2 @ 2.38' },
    ];
    render(<AgentOpsDrawer state={openState('momentum_fr')} log={OPS_ONLY} />);
    await user.click(screen.getByRole('tab', { name: /logs/i }));
    await user.click(screen.getByRole('radio', { name: /^all$/i })); // opt into telemetry
    const log = screen.getByTestId('log');
    expect(within(log).getByText('OPS')).toBeInTheDocument();
    expect(within(log).getByText(/non-scoring/i)).toBeInTheDocument(); // telemetry explicitly non-scored
    expect(within(log).queryByRole('button')).toBeNull(); // no actionable/score affordance
    expect(within(log).queryByText(/\bCLV\b/)).toBeNull(); // never presented as CLV/scored
  });

  it('degrades a BYOA minimal runtime with — for absent optional fields (AC-030)', () => {
    render(
      <AgentOpsDrawer
        state={openState('byoa_hermes')}
        overviewByAgent={{ byoa_hermes: overview({
          agent_id: 'byoa_hermes', run_id: null, latest_model_latency_ms: null,
          latest_model_tokens: null, last_action: null, schema_valid: null, source: 'BYOA',
        }) }}
      />,
    );
    const o = screen.getByTestId('ops-overview');
    expect(within(o).getAllByText('—').length).toBeGreaterThanOrEqual(2);
  });

  it('keeps Pause/Resume/Rotate-creds DISABLED with an honest note (no fabricated pause — the runtime has no such endpoint)', async () => {
    // Even for an owned RUNNING instance, the buttons with no real endpoint stay disabled.
    getInstances.mockResolvedValue([runningInstance]);
    render(<AgentOpsDrawer state={openState('momentum_fr')} overviewByAgent={{ momentum_fr: overview() }} />);
    await screen.findByRole('button', { name: /^kill$/i });
    for (const name of [/pause/i, /resume/i, /rotate/i]) {
      expect(screen.getByRole('button', { name })).toBeDisabled();
    }
    // honest note: the runtime supports shutdown-cancel only (never claims a pause is coming/available)
    expect(screen.getByText(/shutdown-cancel only/i)).toBeInTheDocument();
    // the audit line is TRUTHFUL and present-tense (Kill IS wired now) — never edits scored evidence.
    expect(screen.getByText(/audited and never edit scored evidence/i)).toBeInTheDocument();
    expect(screen.queryByText(/control-plane wiring lands in a later phase/i)).toBeNull();
  });

  it('the audit copy NEGATES editing scored evidence and never positively claims to reset/modify it (honesty)', async () => {
    getInstances.mockResolvedValue([runningInstance]);
    render(<AgentOpsDrawer state={openState('momentum_fr')} />);
    const audit = await screen.findByText(/audited and never edit scored evidence/i);
    // the claim must be a NEGATION ("never edit scored evidence"), not a positive edit/reset claim.
    expect(audit.textContent).toMatch(/never edit scored evidence/i);
    expect(audit.textContent).not.toMatch(/\breset|\bmodif(?:y|ies) (?:the )?(?:scored )?evidence/i);
  });

  // --- F-7 RED #1: Kill → confirm → POST → terminal state; dismiss → no POST ---
  it('Kill opens a confirm dialog; dismissing it fires NO POST', async () => {
    const user = userEvent.setup();
    getInstances.mockResolvedValue([runningInstance]);
    render(<AgentOpsDrawer state={openState('momentum_fr')} />);
    const killBtn = await screen.findByRole('button', { name: /^kill$/i });
    await waitFor(() => expect(killBtn).toBeEnabled());

    await user.click(killBtn);
    const dialog = await screen.findByRole('alertdialog');
    expect(dialog).toBeInTheDocument();
    expect(killInstance).not.toHaveBeenCalled(); // dialog only — nothing fired yet

    await user.click(within(dialog).getByRole('button', { name: /cancel/i }));
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(killInstance).not.toHaveBeenCalled(); // dismissed → NO POST
  });

  it('Kill → confirm fires the POST and reflects the TERMINAL run state from GET .../status', async () => {
    const user = userEvent.setup();
    getInstances.mockResolvedValue([runningInstance]);
    getInstanceStatus
      .mockResolvedValueOnce(runningStatus) // initial gate → running (Kill enabled)
      .mockResolvedValueOnce({ ...runningStatus, run_state: 'cancelled', killed: true }); // post-kill
    render(<AgentOpsDrawer state={openState('momentum_fr')} />);
    const killBtn = await screen.findByRole('button', { name: /^kill$/i });
    await waitFor(() => expect(killBtn).toBeEnabled());

    await user.click(killBtn);
    const dialog = await screen.findByRole('alertdialog');
    await user.click(within(dialog).getByRole('button', { name: /kill run|confirm/i }));

    await waitFor(() => expect(killInstance).toHaveBeenCalledWith('inst_1')); // POST fired
    expect(await screen.findByText(/cancelled/i)).toBeInTheDocument(); // terminal reflected
    expect(screen.getByRole('button', { name: /^kill$/i })).toBeDisabled(); // no re-kill
  });

  // --- F-7 RED #2: non-owner / non-running → destructive buttons blocked, NO POST ---
  it('a NON-owner (no owned instance for the agent) → Kill is disabled and no POST is possible', async () => {
    getInstances.mockResolvedValue([]); // owner owns nothing matching this agent
    render(<AgentOpsDrawer state={openState('momentum_fr')} />);
    const killBtn = await screen.findByRole('button', { name: /^kill$/i });
    expect(killBtn).toBeDisabled();
    expect(killInstance).not.toHaveBeenCalled();
  });

  it('a NON-running (sealed) instance → Kill is disabled; clicking fires no POST', async () => {
    const user = userEvent.setup();
    getInstances.mockResolvedValue([{ ...runningInstance, status: 'sealed' }]);
    getInstanceStatus.mockResolvedValue({ ...runningStatus, run_state: 'sealed' });
    render(<AgentOpsDrawer state={openState('momentum_fr')} />);
    const killBtn = await screen.findByRole('button', { name: /^kill$/i });
    await waitFor(() => expect(killBtn).toBeDisabled());
    await user.click(killBtn); // disabled → nothing happens
    expect(screen.queryByRole('alertdialog')).toBeNull();
    expect(killInstance).not.toHaveBeenCalled();
  });

  // --- F-7 RED #3: a failed kill surfaces a VISIBLE error, never a false success ---
  it('a FAILED kill surfaces a visible error and does NOT show a terminal/success state', async () => {
    const user = userEvent.setup();
    getInstances.mockResolvedValue([runningInstance]);
    killInstance.mockRejectedValue(new Error('POST /agents/instances/inst_1/kill failed: 409'));
    render(<AgentOpsDrawer state={openState('momentum_fr')} />);
    const killBtn = await screen.findByRole('button', { name: /^kill$/i });
    await waitFor(() => expect(killBtn).toBeEnabled());

    await user.click(killBtn);
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: /kill run|confirm/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/409|failed/i); // VISIBLE error
    expect(screen.queryByText(/cancelled/i)).toBeNull(); // never a fabricated terminal
  });

  // --- F-7 RED #4: Disable execution routes to the competition kill-switch ---
  it('Disable execution (with a competition context) routes to POST /competitions/{id}/kill-switch', async () => {
    const user = userEvent.setup();
    getInstances.mockResolvedValue([runningInstance]);
    render(<AgentOpsDrawer state={openState('momentum_fr')} competitionId="comp_1" />);
    const disableBtn = await screen.findByRole('button', { name: /disable execution/i });
    await waitFor(() => expect(disableBtn).toBeEnabled());

    await user.click(disableBtn);
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: /disable|confirm/i }));

    await waitFor(() => expect(armCompetitionKillSwitch).toHaveBeenCalledWith('comp_1'));
  });

  it('Disable execution stays DISABLED when there is no competition context (never fabricates a competition id)', async () => {
    getInstances.mockResolvedValue([runningInstance]);
    render(<AgentOpsDrawer state={openState('momentum_fr')} />); // no competitionId prop
    const disableBtn = await screen.findByRole('button', { name: /disable execution/i });
    expect(disableBtn).toBeDisabled();
  });

  // --- F-7: demo mode never fakes a kill ---
  it('DEMO mode (mock on) keeps Kill disabled and never fires a kill (no faked action)', async () => {
    isMockEnabled.mockReturnValue(true);
    render(<AgentOpsDrawer state={openState('momentum_fr')} overviewByAgent={{ momentum_fr: overview() }} />);
    const killBtn = await screen.findByRole('button', { name: /^kill$/i });
    expect(killBtn).toBeDisabled();
    expect(killInstance).not.toHaveBeenCalled();
  });

  it('handles an unknown agent gracefully — honest empty, no crash (REQ-031)', async () => {
    render(<AgentOpsDrawer state={openState('does_not_exist')} />);
    expect(await screen.findByText(/no runtime data/i)).toBeInTheDocument();
  });

  it('is a proper modal dialog — role on the content panel, aria-modal, Escape closes, tabs link a tabpanel (a11y)', async () => {
    const user = userEvent.setup();
    const state = openState('momentum_fr');
    render(<AgentOpsDrawer state={state} overviewByAgent={{ momentum_fr: overview() }} />);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(within(dialog).getByText(/RUNTIME OBSERVABILITY/i)).toBeInTheDocument();
    expect(screen.getAllByRole('tab')).toHaveLength(2);
    expect(screen.getByRole('tabpanel')).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(state.close).toHaveBeenCalled();
  });
});
