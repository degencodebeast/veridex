import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AgentStudioScreen } from '@/components/screens/AgentStudioScreen';

describe('AgentStudioScreen (REQ-018 / AC-007 / SEC-006/007/009)', () => {
  it('locks LLM mode for value_clv and prevents selecting it; momentum unlocks it (AC-007)', async () => {
    const user = userEvent.setup();
    render(<AgentStudioScreen />);
    // default archetype is value_clv -> LLM locked (b1 SegmentedControl emits aria-disabled only when locked)
    const llm = screen.getByRole('radio', { name: /LLM/ });
    expect(llm).toHaveAttribute('aria-disabled', 'true');
    await user.click(llm);
    expect(screen.getByRole('radio', { name: /numeric/i })).toHaveAttribute('aria-checked', 'true');
    // switch archetype to momentum -> LLM unlocks (no aria-disabled=true)
    await user.selectOptions(screen.getByLabelText(/archetype/i), 'momentum');
    expect(screen.getByRole('radio', { name: /LLM/ })).not.toHaveAttribute('aria-disabled', 'true');
  });

  it('snaps a selected LLM mode back to numeric when archetype switches to a locked one (AC-007 snap-back)', async () => {
    const user = userEvent.setup();
    render(<AgentStudioScreen />);
    await user.selectOptions(screen.getByLabelText(/archetype/i), 'momentum');
    await user.click(screen.getByRole('radio', { name: /LLM/ }));
    expect(screen.getByRole('radio', { name: /LLM/ })).toHaveAttribute('aria-checked', 'true');
    await user.selectOptions(screen.getByLabelText(/archetype/i), 'value_clv');
    expect(screen.getByRole('radio', { name: /numeric/i })).toHaveAttribute('aria-checked', 'true');
  });

  it('keeps sections 02 and 03 mutually exclusive with continuous 01-05 numbering', async () => {
    const user = userEvent.setup();
    render(<AgentStudioScreen />);
    // value_clv => numeric: section 03 active, 02 a "not applicable" stub
    expect(screen.getByTestId('section-03')).not.toHaveAttribute('data-inactive', 'true');
    expect(screen.getByTestId('section-02')).toHaveAttribute('data-inactive', 'true');
    expect(within(screen.getByTestId('section-02')).getByText(/not applicable in this mode/i)).toBeInTheDocument();
    // switch to momentum + LLM => section 02 active, 03 a stub
    await user.selectOptions(screen.getByLabelText(/archetype/i), 'momentum');
    await user.click(screen.getByRole('radio', { name: /LLM/ }));
    expect(screen.getByTestId('section-02')).not.toHaveAttribute('data-inactive', 'true');
    expect(screen.getByTestId('section-03')).toHaveAttribute('data-inactive', 'true');
  });

  it('fences the LLM SportsActionTypes as NOT AN INPUT TO SCORE (SEC-007)', async () => {
    const user = userEvent.setup();
    render(<AgentStudioScreen />);
    await user.selectOptions(screen.getByLabelText(/archetype/i), 'momentum');
    await user.click(screen.getByRole('radio', { name: /LLM/ }));
    const shell = screen.getByTestId('section-02');
    expect(within(shell).getByText(/NOT AN INPUT TO SCORE/i)).toBeInTheDocument();
    for (const t of ['WAIT', 'FLAG_VALUE', 'FOLLOW_MOMENTUM', 'FADE', 'WIDEN_OR_SUSPEND']) {
      expect(within(shell).getByText(t)).toBeInTheDocument();
    }
  });

  it('shows a sticky Preflight Preview and pins config on commit (SEC-009)', async () => {
    const user = userEvent.setup();
    const onPin = vi.fn();
    render(<AgentStudioScreen onPin={onPin} />);
    const preview = screen.getByTestId('preflight');
    expect(within(preview).getByText(/config_hash/i)).toBeInTheDocument();
    expect(within(preview).getByText(/allow|deny/i)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /pin config & queue run/i }));
    expect(onPin).toHaveBeenCalledWith(expect.objectContaining({ config_hash: expect.any(String), policy_hash: expect.any(String) }));
  });

  it('an edit produces a REVIEWABLE before→after diff, not a silent live mutation (#4 / SEC-009)', async () => {
    const user = userEvent.setup();
    render(<AgentStudioScreen />);
    const diff = screen.getByTestId('config-diff');
    expect(within(diff).getByText(/no pending changes/i)).toBeInTheDocument(); // clean baseline
    await user.selectOptions(screen.getByLabelText(/archetype/i), 'momentum');
    // the change is shown as a structured before→after patch (reviewable), applied as a new version on pin
    const row = within(diff).getByTestId('diff-archetype');
    expect(row).toHaveTextContent(/value_clv/); // before
    expect(row).toHaveTextContent(/momentum/);  // after
    expect(within(diff).getByText(/new (pinned )?version/i)).toBeInTheDocument(); // never a live mutation
  });

  it('is READ-ONLY during a scored run — no editable config affordances mid-run (#4 / SEC-006)', () => {
    render(<AgentStudioScreen running />);
    expect(screen.getByLabelText(/archetype/i)).toBeDisabled(); // archetype not editable
    expect(screen.queryByRole('radio', { name: /numeric/i })).toBeNull(); // mode control not editable
    expect(screen.queryByRole('button', { name: /pin config/i })).toBeNull(); // can't re-pin mid-run
    expect(screen.getByText(/read-only during a scored run/i)).toBeInTheDocument();
  });
});
