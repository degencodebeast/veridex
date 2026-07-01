import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { CreateCompetitionScreen } from '@/components/screens/CreateCompetitionScreen';
import { MARKET_FAMILY_KEYS } from '@/lib/catalog';
import { GLOSSARY } from '@/lib/glossary';

describe('CreateCompetitionScreen (REQ-015 / SEC-009)', () => {
  it('pins law/policy/proof/exec before entry and reflects the type choice', async () => {
    const user = userEvent.setup();
    render(<CreateCompetitionScreen />);
    const pinned = screen.getByTestId('pinned-config');
    expect(pinned).toHaveTextContent(/proof/i);
    // "Replay" exists in both Type and Source pickers — scope to the Source radiogroup.
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

  it('commits the pinned config and routes to the cockpit', async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    render(<CreateCompetitionScreen onCommit={onCommit} />);
    await user.click(screen.getByRole('button', { name: /commit & enter/i }));
    expect(onCommit).toHaveBeenCalledWith(
      expect.objectContaining({
        competition_type: expect.any(String), execution_mode: expect.any(String), proof_mode: expect.any(String),
      }),
    );
  });

  it('commits EXACTLY the config that was pinned/shown — change source, commit reflects it (SEC-009)', async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    render(<CreateCompetitionScreen onCommit={onCommit} />);
    const sourceGroup = screen.getByRole('radiogroup', { name: /source mode/i });
    await user.click(within(sourceGroup).getByRole('radio', { name: 'Replay' }));
    await user.click(screen.getByRole('button', { name: /commit & enter/i }));
    // The derived proof_mode (shown as pinned) must travel with the commit (SEC-009 "commit what's pinned").
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ source_mode: 'replay', proof_mode: 'reproducible' }));
  });
});

// ── V5 fidelity: type cards · fixture/scoring · market scope · SUMMARY sidebar ──
describe('CreateCompetitionScreen V5 (wizard density · honest pins)', () => {
  it('renders the 4 REAL competition_type cards; selecting one pins it into the commit (SEC-009)', async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    render(<CreateCompetitionScreen onCommit={onCommit} />);
    const cards = screen.getByTestId('type-cards');
    (['live_arena', 'replay_arena', 'head_to_head', 'prize_vault_challenge'] as const).forEach((t) =>
      expect(within(cards).getByTestId(`type-${t}`)).toBeInTheDocument());
    await user.click(within(cards).getByTestId('type-head_to_head'));
    await user.click(screen.getByRole('button', { name: /commit & enter/i }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ competition_type: 'head_to_head' }));
  });

  it('market-scope options are EXACTLY the real MARKET_FAMILY_KEYS — never invented markets', () => {
    render(<CreateCompetitionScreen />);
    const scope = screen.getByTestId('market-scope');
    MARKET_FAMILY_KEYS.forEach((k) => expect(within(scope).getByTestId(`market-${k}`)).toBeInTheDocument());
    // no fabricated market families offered
    expect(within(scope).queryByText(/BTTS|correct score|first goalscorer|half-time/i)).toBeNull();
  });

  it('composes market_scope from the selected fixture + real families; scoring window honest-empty when unset', async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    render(<CreateCompetitionScreen onCommit={onCommit} />);
    const summary = screen.getByTestId('pinned-config');
    // default fixture (FRA v BRA) + families compose into the pinned market_scope
    expect(within(summary).getByTestId('summary-market-scope')).toHaveTextContent(/FRA v BRA/i);
    // scoring window is optional → honest label when unset, NOT a fabricated window
    expect(within(summary).getByTestId('summary-scoring-window')).toHaveTextContent(/full match/i);
    await user.click(screen.getByRole('button', { name: /commit & enter/i }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({
      market_scope: expect.stringMatching(/FRA v BRA/i), scoring_window: null,
    }));
  });

  it('SUMMARY pins the real CompetitionConfig fields POST freezes; REPLAY source shows REPLAY, never LIVE', async () => {
    const user = userEvent.setup();
    render(<CreateCompetitionScreen />);
    const summary = screen.getByTestId('pinned-config');
    ['summary-type', 'summary-source', 'summary-exec', 'summary-market-scope', 'summary-scoring-window']
      .forEach((id) => expect(within(summary).getByTestId(id)).toBeInTheDocument());
    // source_mode axis is separate from competition_type: choosing Replay pins REPLAY, never LIVE.
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
    expect(within(summary).getByTestId('config-pinned-caption'))
      .toHaveTextContent(/frozen at create.*Proof Card/i);
    // still no fake digest (the "Config pinned" value must not regress into a hash)
    expect(within(summary).getByTestId('summary-config-pinned')).not.toHaveTextContent(/0x[0-9a-f]{6,}/i);
  });

  it('InfoTip copy is single-sourced from lib/glossary.ts — no per-screen microcopy drift', () => {
    render(<CreateCompetitionScreen />);
    // the on-screen tooltip text for ALL 4 wired doctrine terms MUST equal the glossary verbatim.
    expect(screen.getByText(GLOSSARY.source_mode.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.execution_mode.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.proof_mode.definition)).toBeInTheDocument();
    expect(screen.getByText(GLOSSARY.config_pinned.definition)).toBeInTheDocument();
  });
});
