import { describe, it, expect, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { CreateCompetitionScreen, type LaunchApi } from '@/components/screens/CreateCompetitionScreen';
import type { DeployedInstance } from '@/lib/api';

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
  mkInstance({ instance_id: 'inst-a', agent_id: 'clv-hunter' }),
  mkInstance({ instance_id: 'inst-b', agent_id: 'momentum-v3' }),
];
function okApi(): LaunchApi {
  return {
    create: vi.fn().mockResolvedValue({ competition_id: 'c_1', status: 'draft' }),
    register: vi.fn().mockResolvedValue({ agent_id: 'a', config_hash: 'h', proof_mode: 'reproducible' }),
    start: vi.fn().mockResolvedValue({ competition_id: 'c_1', status: 'finalized', run_id: 'run_1' }),
  };
}

describe('CreateCompetitionScreen — Launch carries the authoritative pack_id + fixture_id', () => {
  it('clicking Launch fires launchApi.create with the deep-linked pack_id + fixture_id', async () => {
    // Mock OFF (default): the fixture-picker snap effect is skipped, so the deep-linked fixtureId is
    // preserved verbatim (18209181 is not one of the demo FIXTURES). Roster load is independent of mock.
    const user = userEvent.setup();
    const api = okApi();
    render(
      <CreateCompetitionScreen
        connected
        initialFixtureId={18209181}
        packId="curated"
        loadInstances={vi.fn().mockResolvedValue(TWO_INSTANCES)}
        launchApi={api}
      />,
    );
    await user.click(await screen.findByTestId('roster-inst-a'));
    await user.click(await screen.findByTestId('roster-inst-b'));
    await user.click(screen.getByTestId('launch-button'));
    await waitFor(() =>
      expect(api.create).toHaveBeenCalledWith(
        expect.objectContaining({ pack_id: 'curated', fixture_id: 18209181, roster_size: 2 }),
      ),
    );
  });

  it('preserves an EXPLICIT fixture_id=0 (a valid, presence-distinct id) rather than silently omitting it', async () => {
    // 0 is a backend-valid fixture id distinct from "omitted" (None → server picks the pack minimum).
    // An explicit deep-linked ?fixture_id=0 must reach launchApi.create as fixture_id:0, never dropped.
    const user = userEvent.setup();
    const api = okApi();
    render(
      <CreateCompetitionScreen
        connected
        initialFixtureId={0}
        packId="curated"
        loadInstances={vi.fn().mockResolvedValue(TWO_INSTANCES)}
        launchApi={api}
      />,
    );
    await user.click(await screen.findByTestId('roster-inst-a'));
    await user.click(await screen.findByTestId('roster-inst-b'));
    await user.click(screen.getByTestId('launch-button'));
    await waitFor(() =>
      expect(api.create).toHaveBeenCalledWith(
        expect.objectContaining({ pack_id: 'curated', fixture_id: 0, roster_size: 2 }),
      ),
    );
  });
});
