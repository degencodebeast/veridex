import { describe, it, expect, vi } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { OperatorDashboardScreen } from '@/components/screens/OperatorDashboardScreen';
import { MY_AGENTS } from '@/lib/fixtures/catalog';
import type { DeployedInstance } from '@/lib/api';

// F-3: "Your Agents" is REBOUND off the MY_AGENTS fixture onto real owned instances (getInstances).
// The screen fetches via an injectable `loadInstances` (defaults to getInstances) so tests drive
// every state with no network. Empty is a NO-OP loader so the non-agent tests never hit fetch.
const NO_INSTANCES = async (): Promise<DeployedInstance[]> => [];

function instance(overrides: Partial<DeployedInstance> = {}): DeployedInstance {
  return {
    instance_id: 'inst_alpha',
    template_id: 'value_clv',
    agent_id: 'studio-value_clv',
    run_id: 'run_evidence_01',
    status: 'running',
    source_mode: 'replay',
    execution_mode: 'paper',
    config_hash: 'c'.repeat(64),
    policy_hash: 'p'.repeat(64),
    operator_id: 'did:privy:owner-1',
    runtime_handle: { runtime_kind: 'agentos', runtime_agent_id: 'aos_1', session_id: 'sess_1', run_id: 'run_evidence_01' },
    last_failure_reason: null,
    market_allowlist: ['moneyline'],
    venue_allowlist: ['polymarket'],
    created_at: '2026-07-17T00:00:00Z',
    ...overrides,
  };
}

function agentsPanel() {
  return screen.getByRole('heading', { name: /your agents/i }).closest('section') as HTMLElement;
}

describe('OperatorDashboardScreen (REQ-012 / SEC-008)', () => {
  it('renders the personal sections and the primary buttons (connected)', () => {
    render(<OperatorDashboardScreen connected loadInstances={NO_INSTANCES} />);
    expect(screen.getByRole('heading', { name: /your agents/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /your runs/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /your rewards/i })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /new agent/i })).toHaveAttribute('href', '/studio');
    expect(screen.getByRole('link', { name: /join competition/i })).toHaveAttribute('href', '/competitions');
  });

  it('does NOT leak operator-private data when disconnected — fail-closed, ABSENT from DOM (#6 / SEC-008)', () => {
    render(<OperatorDashboardScreen connected={false} loadInstances={NO_INSTANCES} />);
    // Private sections are not in the DOM at all (not merely visually hidden).
    expect(screen.queryByRole('heading', { name: /your agents/i })).toBeNull();
    expect(screen.queryByRole('heading', { name: /your runs/i })).toBeNull();
    expect(screen.queryByTestId('your-rewards')).toBeNull();
    expect(screen.queryByTestId('alerts-rail')).toBeNull();
    // No private agent identity leaks.
    MY_AGENTS.forEach((a) => expect(screen.queryByText(a.agent_name)).toBeNull());
    // Honest connect prompt instead.
    expect(screen.getByText(/connect.*wallet/i)).toBeInTheDocument();
  });

  it('connect-gate renders a WORKING login control that fires onConnect (dead-gate fix)', async () => {
    const user = userEvent.setup();
    const onConnect = vi.fn();
    render(<OperatorDashboardScreen connected={false} onConnect={onConnect} loadInstances={NO_INSTANCES} />);
    const gate = screen.getByTestId('connect-gate');
    const btn = within(gate).getByRole('button', { name: /connect wallet|log in/i });
    await user.click(btn);
    expect(onConnect).toHaveBeenCalledTimes(1);
    // SEC-008 still holds: private sections are ABSENT even with a login affordance present.
    expect(screen.queryByRole('heading', { name: /your agents/i })).toBeNull();
  });

  // ---- F-3: Your Agents renders REAL owned instances, never the MY_AGENTS fixture ----

  it('renders real owned instances (instance_id / template / status), NOT the MY_AGENTS fixture names', async () => {
    render(
      <OperatorDashboardScreen
        connected
        loadInstances={async () => [
          instance({ instance_id: 'inst_alpha', template_id: 'value_clv', status: 'running' }),
          instance({ instance_id: 'inst_beta', template_id: 'momentum', status: 'sealed', agent_id: 'studio-momentum' }),
        ]}
      />,
    );
    const agents = agentsPanel();
    expect(await within(agents).findByText('inst_alpha')).toBeInTheDocument();
    expect(within(agents).getByText('inst_beta')).toBeInTheDocument();
    expect(within(agents).getAllByText(/value_clv|momentum/).length).toBeGreaterThanOrEqual(2);
    // status appears in both the row sub-line and the lifecycle pill — presence, not uniqueness.
    expect(within(agents).getAllByText(/running/i).length).toBeGreaterThanOrEqual(1);
    expect(within(agents).getAllByText(/sealed/i).length).toBeGreaterThanOrEqual(1);
    // Owner-scoped rows link to the /instances/[id] identity, not the public /agents profile.
    expect(within(agents).getByRole('link', { name: /inst_alpha/i })).toHaveAttribute('href', '/instances/inst_alpha');
    // The MY_AGENTS fixture must be GONE from the agents list (rebound, not fallback).
    MY_AGENTS.forEach((a) => expect(within(agents).queryByText(a.agent_name)).toBeNull());
  });

  it('live-empty: honest empty state, NEVER a MY_AGENTS fixture fallback (T-2 fixture prohibition)', async () => {
    render(<OperatorDashboardScreen connected loadInstances={NO_INSTANCES} />);
    const agents = agentsPanel();
    expect(await within(agents).findByText(/no deployed agents/i)).toBeInTheDocument();
    // The fixture entity names are ABSENT from the agents list (no live-rendered fixture fallback).
    // Scoped to the agents panel: two MY_AGENTS names also live in the out-of-scope "Your Runs"
    // fixture panel, so the F-3 property under test is precisely "Your Agents never falls back."
    MY_AGENTS.forEach((a) => expect(within(agents).queryByText(a.agent_name)).toBeNull());
  });

  it('load failure (401/403): honest error state, NEVER a MY_AGENTS fixture fallback', async () => {
    render(
      <OperatorDashboardScreen
        connected
        loadInstances={async () => { throw new Error('unauthorized'); }}
      />,
    );
    const agents = agentsPanel();
    await waitFor(() => expect(within(agents).getByText(/couldn.t load|could not load/i)).toBeInTheDocument());
    // No fixture fallback in the agents list even on a failed load (T-2).
    MY_AGENTS.forEach((a) => expect(within(agents).queryByText(a.agent_name)).toBeNull());
  });

  it('opens the runtime drawer from a Your-Agents row (REQ-012 -> REQ-030)', async () => {
    const user = userEvent.setup();
    const onOpenRuntime = vi.fn();
    render(
      <OperatorDashboardScreen
        connected
        onOpenRuntime={onOpenRuntime}
        loadInstances={async () => [instance()]}
      />,
    );
    await within(agentsPanel()).findByText('inst_alpha');
    await user.click(screen.getAllByRole('button', { name: /runtime/i })[0]);
    expect(onOpenRuntime).toHaveBeenCalled();
  });

  it('labels rewards with honest payout states, never implying paid (SEC-008)', () => {
    render(<OperatorDashboardScreen connected loadInstances={NO_INSTANCES} />);
    const rewards = screen.getByTestId('your-rewards');
    expect(within(rewards).getByText(/design target/i)).toBeInTheDocument();
    expect(within(rewards).getAllByText(/pending/i).length).toBeGreaterThanOrEqual(1);
    expect(within(rewards).queryByText(/^paid$/i)).toBeNull();
  });

  it('shows a FAILED payout as a distinct negative state, never a misleading pending badge (honesty)', () => {
    render(
      <OperatorDashboardScreen
        connected
        loadInstances={NO_INSTANCES}
        rewards={[{ competition_id: 'x', title: 'X Cup', amount_label: '— (failed)', payout_state: 'failed' }]}
      />,
    );
    const rewards = screen.getByTestId('your-rewards');
    const failed = within(rewards).getByText('failed');
    expect(failed).toHaveAttribute('data-payout', 'failed'); // distinct negative span, not a Badge
    expect(within(rewards).queryByText(/pending/i)).toBeNull(); // a failure is NOT shown as pending
  });

  it('shows the alerts rail with kill/deny/hold items', () => {
    render(<OperatorDashboardScreen connected loadInstances={NO_INSTANCES} />);
    const rail = screen.getByTestId('alerts-rail');
    expect(within(rail).getByText('DENY')).toBeInTheDocument();
    expect(within(rail).getByText('HOLD')).toBeInTheDocument();
  });
});
