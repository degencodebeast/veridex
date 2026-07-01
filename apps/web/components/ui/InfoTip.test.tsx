import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { InfoTip } from '@/components/ui/InfoTip';

afterEach(cleanup);

describe('InfoTip (accessible glossary primitive)', () => {
  it('is a focusable button with an accessible name, describing the popover via aria-describedby', () => {
    render(<InfoTip label="CLV">entry vs later closing line</InfoTip>);
    const btn = screen.getByRole('button', { name: /what is clv/i });
    const tip = screen.getByRole('tooltip');
    // the trigger is described BY the popover content (real a11y wiring, not a title-only tooltip)
    expect(btn).toHaveAttribute('aria-describedby', tip.id);
    expect(tip).toHaveTextContent(/entry vs later closing line/i);
  });

  it('opens on keyboard focus and closes on Escape (keyboard-reachable, not hover-only)', () => {
    render(<InfoTip label="CLV">definition text</InfoTip>);
    const btn = screen.getByRole('button', { name: /what is clv/i });
    const tip = screen.getByRole('tooltip');
    expect(tip).toHaveAttribute('data-open', 'false');
    fireEvent.focus(btn);
    expect(tip).toHaveAttribute('data-open', 'true'); // opens on focus
    fireEvent.keyDown(btn, { key: 'Escape' });
    expect(tip).toHaveAttribute('data-open', 'false'); // Escape closes
  });

  it('opens on tap/click and STAYS open — not a self-closing toggle (reliable for touch/Mobile)', () => {
    render(<InfoTip label="CLV">definition text</InfoTip>);
    const btn = screen.getByRole('button', { name: /what is clv/i });
    const tip = screen.getByRole('tooltip');
    fireEvent.click(btn);
    expect(tip).toHaveAttribute('data-open', 'true'); // tap opens
    fireEvent.click(btn);
    expect(tip).toHaveAttribute('data-open', 'true'); // re-tap does NOT toggle it closed
  });

  it('closes on an outside tap/click (coherent dismissal for touch)', () => {
    render(<div><InfoTip label="CLV">definition text</InfoTip></div>);
    const btn = screen.getByRole('button', { name: /what is clv/i });
    const tip = screen.getByRole('tooltip');
    fireEvent.click(btn);
    expect(tip).toHaveAttribute('data-open', 'true');
    fireEvent.mouseDown(document.body); // tap outside the tip
    expect(tip).toHaveAttribute('data-open', 'false');
  });
});
