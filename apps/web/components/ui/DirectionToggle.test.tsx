import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DirectionToggle } from '@/components/ui/DirectionToggle';

beforeEach(() => { localStorage.clear(); document.documentElement.removeAttribute('data-direction'); });

describe('DirectionToggle', () => {
  it('toggles the document direction to B', async () => {
    const user = userEvent.setup();
    render(<DirectionToggle />);
    await user.click(screen.getByRole('radio', { name: /SaaS/i }));
    expect(document.documentElement.getAttribute('data-direction')).toBe('b');
  });
});
