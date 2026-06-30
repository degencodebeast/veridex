import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AppShell } from '@/components/layout/AppShell';

vi.mock('next/navigation', () => ({ usePathname: () => '/' }));

describe('AppShell', () => {
  it('renders the top nav, the wallet chip, and the page content region', () => {
    render(<AppShell><p>screen body</p></AppShell>);
    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /OP 9xQe/i })).toBeInTheDocument();
    expect(screen.getByRole('main')).toBeInTheDocument();
    expect(screen.getByText('screen body')).toBeInTheDocument();
  });

  it('restores the persisted visual Direction app-wide on mount (CON-001 — every route, not just the toggle screens)', () => {
    localStorage.setItem('veridex.direction', 'b');
    try {
      render(<AppShell><p>x</p></AppShell>);
      expect(document.documentElement.getAttribute('data-direction')).toBe('b');
    } finally {
      localStorage.clear();
      document.documentElement.removeAttribute('data-direction');
    }
  });
});
