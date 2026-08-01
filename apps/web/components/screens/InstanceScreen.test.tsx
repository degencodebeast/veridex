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

  it('renders an honest absence when the replaceable AgentOS session_id is null', async () => {
    render(<InstanceScreen instanceId="inst_mine" load={async () => instance({
      runtime_handle: { runtime_kind: 'agentos', runtime_agent_id: 'aos_1', session_id: null, run_id: 'run_evidence_01' },
    })} />);

    await screen.findByText('inst_mine');
    expect(screen.getByText('—')).toBeInTheDocument();
    expect(screen.getByText(/replaceable AgentOS handle/i)).toBeInTheDocument();
    expect(screen.getByText(/authoritative Veridex evidence identity/i)).toBeInTheDocument();
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

  it('renders the CURATED fixture + market label for a maker instance, without hiding the raw id', async () => {
    render(<InstanceScreen instanceId="inst_maker" load={async () => instance({
      instance_id: 'inst_maker',
      market_allowlist: ['pmxt:18209181:home_win'],
      fixture_id: 18209181, fixture_label: 'France v Morocco', market_label: 'Home win',
      replay_pack_content_hash: 'f16c3853pack', replay_pack_id: 'pmxt-txline-mm-18209181-v1',
      maker_tape_ref: 'pmxt-txline-mm-18209181-v1', maker_tape_content_hash: '19b314abtape',
    })} />);
    await screen.findByText('inst_maker');
    // The human label reads prominently in the header…
    expect(screen.getByTestId('instance-fixture-label')).toHaveTextContent('France v Morocco · Home win');
    // …and the raw id remains visible in the Scope row (augment, never replace).
    expect(screen.getByTestId('instance-fixture-row')).toHaveTextContent('18209181');
    // The replay PACK hash and the maker TAPE hash are surfaced as DISTINCT identities, correctly
    // labeled — the pack hash is never presented as the tape hash.
    const packHash = screen.getByTestId('instance-pack-hash');
    expect(packHash).toHaveTextContent('replay pack content_hash');
    expect(packHash).toHaveTextContent('f16c3853pack');
    expect(screen.getByTestId('instance-pack-id')).toHaveTextContent('pmxt-txline-mm-18209181-v1');
    const tapeHash = screen.getByTestId('instance-maker-tape-hash');
    expect(tapeHash).toHaveTextContent('maker tape content_hash');
    expect(tapeHash).toHaveTextContent('19b314abtape');
    expect(screen.getByTestId('instance-maker-tape-ref')).toHaveTextContent('pmxt-txline-mm-18209181-v1');
  });

  it('a non-MM instance shows the replay pack hash but NO maker tape rows', async () => {
    render(<InstanceScreen instanceId="inst_dir" load={async () => instance({
      instance_id: 'inst_dir',
      fixture_id: 18209181, fixture_label: 'France v Morocco', market_label: 'Home win',
      replay_pack_content_hash: 'f16c3853pack', replay_pack_id: 'pack-directional',
      maker_tape_ref: null, maker_tape_content_hash: null,
    })} />);
    await screen.findByText('inst_dir');
    expect(screen.getByTestId('instance-pack-hash')).toHaveTextContent('f16c3853pack');
    expect(screen.queryByTestId('instance-maker-tape-hash')).toBeNull();
    expect(screen.queryByTestId('instance-maker-tape-ref')).toBeNull();
  });

  it('renders the honest "Fixture {id}" fallback for an unmapped fixture', async () => {
    render(<InstanceScreen instanceId="inst_unmapped" load={async () => instance({
      instance_id: 'inst_unmapped',
      market_allowlist: ['pmxt:999999:away_win'],
      fixture_id: 999999, fixture_label: 'Fixture 999999', market_label: 'Away win',
    })} />);
    await screen.findByText('inst_unmapped');
    expect(screen.getByTestId('instance-fixture-label')).toHaveTextContent('Fixture 999999 · Away win');
  });

  it('a MAKER instance links to the QuoteGuard Ablation and does NOT send run_id to the 404-ing directional proof card', async () => {
    render(<InstanceScreen instanceId="inst_maker" load={async () => instance({
      instance_id: 'inst_maker', template_id: 'quoteguard_mm',
      maker_tape_ref: 'pmxt-txline-mm-18209181-v1', maker_tape_content_hash: '19b314abtape',
    })} />);
    await screen.findByText('inst_maker');
    // The explicit behavior-evidence action points at the maker ablation, keyed by INSTANCE id.
    const ablation = screen.getByTestId('instance-ablation-link');
    expect(ablation).toHaveAttribute('href', '/proof/maker-ablation/inst_maker');
    // run_id is shown as the plain evidence identity — NOT a directional /proof/{run_id} link (that 404s
    // for a maker run). The honest note explains why.
    expect(screen.getByTestId('instance-run-id')).toHaveTextContent('run_evidence_01');
    expect(screen.queryByTestId('instance-run-link')).toBeNull();
    expect(screen.getByTestId('instance-maker-run-note')).toBeInTheDocument();
  });

  it('a DIRECTIONAL instance keeps the working /proof/{run_id} link and shows NO maker ablation action', async () => {
    render(<InstanceScreen instanceId="inst_dir" load={async () => instance({
      instance_id: 'inst_dir', template_id: 'value_clv', maker_tape_ref: null,
    })} />);
    await screen.findByText('inst_dir');
    const runLink = screen.getByTestId('instance-run-link');
    expect(runLink).toHaveAttribute('href', '/proof/run_evidence_01');
    expect(screen.queryByTestId('instance-ablation-link')).toBeNull();
    expect(screen.queryByTestId('instance-run-id')).toBeNull();
    expect(screen.queryByTestId('instance-maker-run-note')).toBeNull();
  });

  it('omits the label rows entirely when the instance carries no CURATED label (e.g. list/demo rows)', async () => {
    render(<InstanceScreen instanceId="inst_mine" load={async () => instance()} />);
    await screen.findByText('inst_mine');
    expect(screen.queryByTestId('instance-fixture-label')).toBeNull();
    expect(screen.queryByTestId('instance-fixture-row')).toBeNull();
    expect(screen.queryByTestId('instance-pack-hash')).toBeNull();
    expect(screen.queryByTestId('instance-maker-tape-hash')).toBeNull();
  });
});
