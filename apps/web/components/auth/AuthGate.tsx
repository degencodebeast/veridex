'use client';
import type { ReactNode } from 'react';
import { usePrivy } from '@privy-io/react-auth';
import styles from './AuthGate.module.css';

// auth-contract@1: the reusable fail-closed wrapper for owner-scoped affordances (e.g. the
// Studio deploy button — wiring AuthGate INTO Studio is a separate task; this component only
// proves the gate). No token/session ⇒ children are NEVER RENDERED, so a gated action (deploy,
// or any owner-scoped call) structurally cannot fire unauthenticated — this is not just a
// disabled button, the affordance is absent from the DOM entirely.
export function AuthGate({ children }: { children: ReactNode }) {
  const { ready, authenticated, login } = usePrivy();

  // Privy not yet initialized: render nothing rather than flash a login prompt an
  // already-authenticated user would never actually see.
  if (!ready) return null;

  if (!authenticated) {
    return (
      <div className={styles.gate} data-testid="auth-login-gate">
        <p className={styles.copy}>Sign in to continue.</p>
        <button type="button" className={styles.login} onClick={login}>Log in</button>
      </div>
    );
  }

  return <>{children}</>;
}
