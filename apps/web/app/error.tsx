'use client';

// Root-segment error boundary (I-5): covers the marketing landing at `/` (outside the (app) group,
// which has its own boundary). Same minimal fallback + retry — re-exported to avoid duplication.
export { default } from './(app)/error';
