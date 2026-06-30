import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { WalletChip } from '@/components/layout/WalletChip';

vi.mock('next/navigation', () => ({ usePathname: () => '/' }));

describe('WalletChip (REQ-002)', () => {
  it('renders the OP wallet chip with a Dashboard affordance', () => {
    render(<WalletChip />);
    expect(screen.getByRole('button', { name: /OP 0x/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Dashboard/i })).toBeInTheDocument();
  });

  it('keeps the dropdown closed until clicked', () => {
    render(<WalletChip />);
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('opens a dropdown with account actions + the prototype screen list', async () => {
    const user = userEvent.setup();
    render(<WalletChip />);
    await user.click(screen.getByRole('button', { name: /OP 0x/i }));
    const menu = screen.getByRole('menu');
    expect(menu).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Operator Dashboard' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Disconnect' })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: 'Design System' })).toBeInTheDocument();
  });
});
