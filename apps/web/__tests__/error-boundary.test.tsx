// I-5 — a route-level error boundary must render a recoverable fallback (not crash the tree) when
// an SSR/render error is thrown, and offer a retry that calls Next's `reset()`.
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ErrorBoundary from '../app/(app)/error';

describe('route error boundary (I-5)', () => {
  it('renders a fallback for a thrown error and retries via reset()', async () => {
    const reset = vi.fn();
    render(<ErrorBoundary error={new Error('boom') as Error & { digest?: string }} reset={reset} />);

    // A visible fallback (not a white-screen crash).
    expect(screen.getByRole('alert')).toBeInTheDocument();

    const retry = screen.getByRole('button', { name: /try again/i });
    await userEvent.click(retry);
    expect(reset).toHaveBeenCalledTimes(1);
  });
});
