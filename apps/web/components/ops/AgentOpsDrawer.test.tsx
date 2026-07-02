import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AgentOpsDrawer } from '@/components/ops/AgentOpsDrawer';
import type { CanonicalLogLine } from '@/lib/catalog';

const openState = (agentId: string) => ({ isOpen: true, agentId, open: vi.fn(), close: vi.fn() });

describe('AgentOpsDrawer (REQ-030..032 / AC-003 / AC-030 / SEC-003)', () => {
  it('renders nothing when closed', () => {
    const { container } = render(
      <AgentOpsDrawer state={{ isOpen: false, agentId: null, open: vi.fn(), close: vi.fn() }} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('shows the read-only NOT SCORED fence header', () => {
    render(<AgentOpsDrawer state={openState('momentum_fr')} />);
    expect(screen.getByText(/RUNTIME OBSERVABILITY · READ-ONLY · NOT SCORED/i)).toBeInTheDocument();
  });

  it('HIDES OPS telemetry by default (canonical-only) and only shows it on explicit opt-in to All (#1 / SEC-003)', async () => {
    const user = userEvent.setup();
    render(<AgentOpsDrawer state={openState('momentum_fr')} />);
    await user.click(screen.getByRole('tab', { name: /logs/i }));
    const log = screen.getByTestId('log');
    // default view is canonical → OPS runtime telemetry is ABSENT (the only source of the literal
    // "OPS" is the channel tag, so a plain substring check is exact here — textContent has no spaces)
    expect(log.textContent).not.toMatch(/OPS/);
    expect(log.textContent).toMatch(/PROOF/);
    // explicit opt-in to the telemetry (All) view → OPS appears
    await user.click(screen.getByRole('radio', { name: /^all$/i }));
    expect(screen.getByTestId('log').textContent).toMatch(/OPS/);
  });

  it('labels POLICY/EXEC derived · non-scoring in canonical view, offering no "proof-only" filter (AC-003)', async () => {
    const user = userEvent.setup();
    render(<AgentOpsDrawer state={openState('momentum_fr')} />);
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
    render(<AgentOpsDrawer state={openState('byoa_hermes')} />);
    const overview = screen.getByTestId('ops-overview');
    expect(within(overview).getAllByText('—').length).toBeGreaterThanOrEqual(2);
  });

  it('exposes lifecycle controls but keeps them DISABLED until control-plane wiring lands, with honest future-tense copy', () => {
    render(<AgentOpsDrawer state={openState('momentum_fr')} />);
    for (const name of [/pause/i, /resume/i, /kill/i, /rotate/i, /disable/i]) {
      const btn = screen.getByRole('button', { name });
      expect(btn).toBeInTheDocument();
      // inert-but-honest: a regression that enables them without real wiring fails here
      expect(btn).toBeDisabled();
    }
    // honest pending label + future-tense intent (NOT a present-tense "are audited" claim)
    expect(screen.getByText(/control-plane wiring lands in a later phase/i)).toBeInTheDocument();
    expect(screen.getByText(/when wired/i)).toBeInTheDocument();
  });

  it('handles an unknown agent gracefully — honest empty, no crash (REQ-031)', () => {
    render(<AgentOpsDrawer state={openState('does_not_exist')} />);
    expect(screen.getByText(/no runtime data/i)).toBeInTheDocument();
  });

  it('is a proper modal dialog — role on the content panel, aria-modal, Escape closes, tabs link a tabpanel (a11y)', async () => {
    const user = userEvent.setup();
    const state = openState('momentum_fr');
    render(<AgentOpsDrawer state={state} />);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    // the dialog role is on the CONTENT panel (it contains the fence header), not the backdrop
    expect(within(dialog).getByText(/RUNTIME OBSERVABILITY/i)).toBeInTheDocument();
    // tab/tabpanel wiring
    expect(screen.getAllByRole('tab')).toHaveLength(2);
    expect(screen.getByRole('tabpanel')).toBeInTheDocument();
    // Escape closes (WalletChip keydown pattern)
    await user.keyboard('{Escape}');
    expect(state.close).toHaveBeenCalled();
  });
});
