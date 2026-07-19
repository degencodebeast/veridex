'use client';

// Route-level error boundary for the (app) group (I-5). App-Router segment errors — including a
// failed SSR data fetch in a child page — render THIS instead of crashing the tree to a blank
// screen. `reset()` re-renders the segment (a retry). Kept intentionally minimal and honest: it
// names that something failed and offers a retry, without fabricating a "healthy" state.
import { useEffect } from 'react';

export default function AppError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Surface for diagnostics; the boundary itself never swallows the failure silently.
    console.error(error);
  }, [error]);

  return (
    <div
      role="alert"
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: '0.75rem',
        alignItems: 'flex-start',
        padding: '2rem',
        color: 'var(--text-1)',
      }}
    >
      <h2 style={{ margin: 0 }}>Something went wrong</h2>
      <p style={{ margin: 0, color: 'var(--text-2)' }}>
        This view failed to load. It may be a transient network or backend issue.
      </p>
      <button
        type="button"
        onClick={() => reset()}
        style={{
          padding: '0.5rem 1rem',
          borderRadius: '6px',
          border: '1px solid var(--border, currentColor)',
          background: 'transparent',
          color: 'inherit',
          cursor: 'pointer',
        }}
      >
        Try again
      </button>
    </div>
  );
}
