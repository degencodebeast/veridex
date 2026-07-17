// auth-contract@1 (frontend half): the injectable access-token seam. Production wires Privy's
// getAccessToken() here via components/auth/AuthProvider; tests inject a FAKE provider — no
// network, no real Privy SDK in unit tests.
//
// The default provider resolves null — fail-closed BY CONSTRUCTION: until something wires a real
// provider (or a test injects a fake one), every owner-scoped call sees "no token" and refuses to
// fire (see lib/api.ts AuthRequiredError).
export type AuthTokenProvider = () => Promise<string | null>;

const NO_TOKEN: AuthTokenProvider = async () => null;

let tokenProvider: AuthTokenProvider = NO_TOKEN;

export function setAuthTokenProvider(fn: AuthTokenProvider): void {
  tokenProvider = fn;
}

// Test-only: restores the fail-closed default so a fake provider from one test never leaks into
// another (test files each get an isolated module registry, but within a file this matters).
export function resetAuthTokenProvider(): void {
  tokenProvider = NO_TOKEN;
}

// Called once to get the current token, and again after a 401 to re-acquire (re-auth) it — the
// SAME seam serves both (auth-contract@1 §401 semantics). Privy's real getAccessToken() already
// refreshes-if-needed, so a plain re-call is the correct "re-acquire" step in production too.
export async function getAuthToken(): Promise<string | null> {
  return tokenProvider();
}
