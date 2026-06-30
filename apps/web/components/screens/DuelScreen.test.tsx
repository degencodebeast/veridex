import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DuelScreen } from '@/components/screens/DuelScreen';

describe('DuelScreen (REQ-023)', () => {
  it('shows two agents on the SAME sealed evidence (one shared evidence hash)', () => {
    render(<DuelScreen />);
    const evidence = screen.getAllByTestId('evidence-hash');
    expect(evidence.length).toBe(1);
    expect(evidence[0]).toHaveTextContent(/sealed evidence/i);
  });

  it('compares CLV and proof side-by-side for the two selected agents', () => {
    render(<DuelScreen />);
    const cards = screen.getAllByTestId('duel-card');
    expect(cards.length).toBe(2);
    cards.forEach((c) => {
      expect(within(c).getByText(/avg clv/i)).toBeInTheDocument();
      expect(within(c).getByTestId('duel-proof')).toBeInTheDocument();
    });
  });

  it('lets the operator switch one side without copying the other agent CLV', async () => {
    const user = userEvent.setup();
    render(<DuelScreen />);
    const left = screen.getByLabelText(/agent a/i);
    await user.selectOptions(left, 'baseline');
    const cards = screen.getAllByTestId('duel-card');
    const leftClv = within(cards[0]).getByTestId('duel-clv').textContent;
    const rightClv = within(cards[1]).getByTestId('duel-clv').textContent;
    expect(leftClv).not.toBe(rightClv); // independent recompute per agent
  });

  it('is an honest factual compare — no fabricated winner/edge, just the CLV gap on shared evidence', () => {
    render(<DuelScreen />);
    expect(screen.queryByText(/winner|\bwins\b|beats|champion|🏆/i)).toBeNull();
    expect(screen.getByText(/key divergence/i)).toBeInTheDocument();
    expect(screen.getByText(/identical sealed evidence/i)).toBeInTheDocument();
  });
});
