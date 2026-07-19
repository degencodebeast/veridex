import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AgentOpsDrawer } from '@/components/ops/AgentOpsDrawer';
import type { CanonicalLogLine, RuntimeOverview } from '@/lib/catalog';
import type { RuntimeEventRecord } from '@/lib/api';

// F-6: the drawer's LIVE data path resolves the owner's instances and polls runtime-events. Both
// readers are mocked so the behaviour tests never touch the network; tests that assert specific data
// inject it via the drawer's props (which override the live hook), keeping them deterministic.
const getInstances = vi.fn();
const getRuntimeEvents = vi.fn();
vi.mock('@/lib/api', async (orig) => ({
  ...(await orig<typeof import('@/lib/api')>()),
  getInstances: (...a: unknown[]) => getInstances(...a),
  getRuntimeEvents: (...a: unknown[]) => getRuntimeEvents(...a),
}));

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
  getInstances.mockResolvedValue([]); // default: owner has no matching instance → honest-empty
  getRuntimeEvents.mockResolvedValue([]);
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

  it('exposes lifecycle controls but keeps them DISABLED until control-plane wiring lands, with honest future-tense copy', () => {
    render(<AgentOpsDrawer state={openState('momentum_fr')} overviewByAgent={{ momentum_fr: overview() }} />);
    for (const name of [/pause/i, /resume/i, /kill/i, /rotate/i, /disable/i]) {
      const btn = screen.getByRole('button', { name });
      expect(btn).toBeInTheDocument();
      // inert-but-honest: a regression that enables them without real wiring fails here (F-7's job)
      expect(btn).toBeDisabled();
    }
    expect(screen.getByText(/control-plane wiring lands in a later phase/i)).toBeInTheDocument();
    expect(screen.getByText(/when wired/i)).toBeInTheDocument();
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
