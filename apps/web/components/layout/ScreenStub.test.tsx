import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ScreenStub } from '@/components/layout/ScreenStub';

describe('ScreenStub (honest placeholder, not fake completion)', () => {
  it('names the screen and the plan that builds it', () => {
    render(<ScreenStub label="Markets" plan="2C Plan C" />);
    expect(screen.getByRole('heading', { name: /Markets/i })).toBeInTheDocument();
    expect(screen.getByText(/2C Plan C/i)).toBeInTheDocument();
  });

  it('exposes no editable affordances (SEC-006/GUD-001)', () => {
    const { container } = render(<ScreenStub label="Agent Studio" plan="2C Plan C" />);
    expect(container.querySelectorAll('input, textarea, select, button').length).toBe(0);
  });
});
