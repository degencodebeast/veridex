import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { CreateCompetitionScreen } from '@/components/screens/CreateCompetitionScreen';

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
      expect.objectContaining({ competition_type: expect.any(String), execution_mode: expect.any(String) }),
    );
  });

  it('commits EXACTLY the config that was pinned/shown — change source, commit reflects it (SEC-009)', async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    render(<CreateCompetitionScreen onCommit={onCommit} />);
    const sourceGroup = screen.getByRole('radiogroup', { name: /source mode/i });
    await user.click(within(sourceGroup).getByRole('radio', { name: 'Replay' }));
    await user.click(screen.getByRole('button', { name: /commit & enter/i }));
    expect(onCommit).toHaveBeenCalledWith(expect.objectContaining({ source_mode: 'replay' }));
  });
});
