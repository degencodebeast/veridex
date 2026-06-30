import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { WalletChip } from '@/components/layout/WalletChip';

vi.mock('next/navigation', () => ({ usePathname: () => '/' }));

const chip = () => screen.getByRole('button', { name: /OP 9xQe/i });

describe('WalletChip (REQ-002, disclosure pattern)', () => {
  it('renders the OP wallet chip with a Dashboard affordance', () => {
    render(<WalletChip />);
    expect(chip()).toBeInTheDocument();
    expect(chip()).toHaveAccessibleName(/Dashboard/i);
  });

  it('keeps the disclosure collapsed until clicked', () => {
    render(<WalletChip />);
    expect(chip()).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('link', { name: 'Operator Dashboard' })).toBeNull();
  });

  it('opens a disclosure with account actions + the prototype screen list', async () => {
    const user = userEvent.setup();
    render(<WalletChip />);
    await user.click(chip());
    expect(chip()).toHaveAttribute('aria-expanded', 'true');
    // Nav targets are links; account actions are buttons (not an application menu).
    expect(screen.getByRole('link', { name: 'Operator Dashboard' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Disconnect' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Design System' })).toBeInTheDocument();
  });

  it('closes on Escape (locks keydown listener cleanup)', async () => {
    const user = userEvent.setup();
    render(<WalletChip />);
    await user.click(chip());
    expect(screen.getByRole('link', { name: 'Operator Dashboard' })).toBeInTheDocument();

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('link', { name: 'Operator Dashboard' })).toBeNull();
    expect(chip()).toHaveAttribute('aria-expanded', 'false');
  });

  it('closes on outside mousedown (locks mousedown listener cleanup)', async () => {
    const user = userEvent.setup();
    render(
      <div>
        <button type="button">outside</button>
        <WalletChip />
      </div>,
    );
    await user.click(chip());
    expect(screen.getByRole('link', { name: 'Operator Dashboard' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'outside' }));
    expect(screen.queryByRole('link', { name: 'Operator Dashboard' })).toBeNull();
    expect(chip()).toHaveAttribute('aria-expanded', 'false');
  });
});
