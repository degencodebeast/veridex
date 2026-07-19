// F-3: the owner-scoped deployed-instance page. Distinct from the PUBLIC /agents strategy profile:
// this renders a REAL deployed instance the caller owns, fetched with a bearer. On a 403/404 it
// renders an honest unauthorized/not-found state — NEVER a fabricated instance.
//
// The screen fetches via an injectable `load` (defaults to getInstance) so tests drive every state
// deterministically with no network.
import { describe, it, expect } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { InstanceScreen } from '@/components/screens/InstanceScreen';
import { ApiError, type DeployedInstance } from '@/lib/api';

function instance(overrides: Partial<DeployedInstance> = {}): DeployedInstance {
  return {
    instance_id: 'inst_mine',
    template_id: 'value_clv',
    agent_id: 'studio-value_clv',
    run_id: 'run_evidence_01',
    status: 'running',
    source_mode: 'replay',
    execution_mode: 'paper',
    config_hash: 'c'.repeat(64),
    policy_hash: 'p'.repeat(64),
    operator_id: 'did:privy:owner-1',
    runtime_handle: { runtime_kind: 'agentos', runtime_agent_id: 'aos_1', session_id: 'sess_replaceable', run_id: 'run_evidence_01' },
    last_failure_reason: null,
    market_allowlist: ['moneyline'],
    venue_allowlist: ['polymarket'],
    created_at: '2026-07-17T00:00:00Z',
    ...overrides,
  };
}

describe('InstanceScreen (owner-scoped deployed-instance identity)', () => {
  it('renders the owned instance: instance_id, the authoritative run_id, and the status', async () => {
    render(<InstanceScreen instanceId="inst_mine" load={async () => instance({ status: 'sealed' })} />);
    expect(await screen.findByText('inst_mine')).toBeInTheDocument();
    expect(screen.getByText(/run_evidence_01/)).toBeInTheDocument();
    // status is surfaced verbatim (in the lifecycle pill)
    expect(screen.getByTestId('instance-status')).toHaveTextContent(/sealed/i);
  });

  it('labels run_id as the authoritative evidence identity and session_id as the replaceable handle', async () => {
    render(<InstanceScreen instanceId="inst_mine" load={async () => instance()} />);
    await screen.findByText('inst_mine');
    // The replaceable AgentOS session handle is present but never presented as the result identity.
    expect(screen.getByText('sess_replaceable')).toBeInTheDocument();
    expect(screen.getByText(/authoritative Veridex evidence identity/i)).toBeInTheDocument();
    expect(screen.getByText(/replaceable AgentOS handle/i)).toBeInTheDocument();
  });

  it('403 (owned by another): renders an honest unauthorized state, NEVER a fabricated instance', async () => {
    render(
      <InstanceScreen
        instanceId="inst_not_mine"
        load={async () => { throw new ApiError(403, 'not yours'); }}
      />,
    );
    await waitFor(() => expect(screen.getByTestId('instance-error')).toBeInTheDocument());
    expect(screen.getByText(/don.t own|not authorized|access/i)).toBeInTheDocument();
    // No fabricated instance identity leaked.
    expect(screen.queryByText('run_evidence_01')).toBeNull();
  });

  it('404 (absent / unowned legacy row): renders an honest not-found state', async () => {
    render(
      <InstanceScreen
        instanceId="inst_ghost"
        load={async () => { throw new ApiError(404, 'not found'); }}
      />,
    );
    await waitFor(() => expect(screen.getByTestId('instance-error')).toBeInTheDocument());
    expect(screen.getByText(/not found/i)).toBeInTheDocument();
  });

  it('a FAILED instance renders the failure reason, never a rosy success/running treatment', async () => {
    render(<InstanceScreen instanceId="inst_failed" load={async () => instance({ status: 'failed', last_failure_reason: 'seal_failed' })} />);
    const fail = await screen.findByTestId('instance-failure');
    expect(fail).toHaveTextContent('seal_failed');
    // status is surfaced verbatim as failed — never coerced to a nicer state.
    expect(screen.getByTestId('instance-status')).toHaveTextContent(/failed/i);
    expect(screen.queryByTestId('instance-error')).toBeNull(); // it IS the instance, just failed
  });

  it('shows a loading state before the fetch resolves', () => {
    render(<InstanceScreen instanceId="inst_mine" load={() => new Promise(() => {})} />);
    expect(screen.getByTestId('instance-loading')).toBeInTheDocument();
  });
});
