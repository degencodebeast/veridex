import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AgentStudioScreen } from '@/components/screens/AgentStudioScreen';

describe('AgentStudioScreen strategy cards (doctrine patch)', () => {
  it('renders six strategy cards with complexity labels incl. heavy-extension', () => {
    render(<AgentStudioScreen />);
    const gallery = screen.getByTestId('strategy-cards');
    for (const label of ['Value-vs-Venue', 'Stale-Line', 'Momentum', 'Contrarian/Fade', 'Arb/Spread', 'QuoteGuard/MM']) {
      expect(within(gallery).getByRole('button', { name: new RegExp(label) })).toBeInTheDocument();
    }
    // precise Phase-3 complexity label (the blurbs also say "heavy extension", so match the label)
    expect(within(gallery).getAllByText(/heavy extension \(phase-3\)/i).length).toBe(2);
  });

  it('Arb/Spread and QuoteGuard/MM are honestly Phase-3 — disabled, never selectable-as-if-built (#T19)', async () => {
    const user = userEvent.setup();
    render(<AgentStudioScreen />);
    const gallery = screen.getByTestId('strategy-cards');
    const arb = within(gallery).getByRole('button', { name: /Arb\/Spread/ });
    const mm = within(gallery).getByRole('button', { name: /QuoteGuard\/MM/ });
    expect(arb).toBeDisabled();
    expect(mm).toBeDisabled();
    // QuoteGuard/MM maps to 'baseline'; clicking the disabled Phase-3 card must NOT select it
    await user.click(mm);
    expect(screen.getByLabelText(/archetype/i)).toHaveValue('value_clv'); // unchanged default
  });

  it('selecting Momentum unlocks LLM via the existing coupling (AC-007 preserved)', async () => {
    const user = userEvent.setup();
    render(<AgentStudioScreen />);
    await user.click(within(screen.getByTestId('strategy-cards')).getByRole('button', { name: /Momentum/ }));
    expect(screen.getByLabelText(/archetype/i)).toHaveValue('momentum');
    // b1 SegmentedControl emits aria-disabled only when locked → unlocked = not 'true'
    expect(screen.getByRole('radio', { name: /LLM/ })).not.toHaveAttribute('aria-disabled', 'true');
  });
});
