'use client';
import { useRouter } from 'next/navigation';
import type { DeployAgentResult } from '@/lib/api';
import { StudioScreen } from '@/components/screens/StudioScreen';
import { AuthGate } from '@/components/auth/AuthGate';
import authGateStyles from '@/components/auth/AuthGate.module.css';

// The Studio DEPLOY affordance is owner-scoped (auth-contract@1, program.json:2711), so it is gated
// behind the real session — an unauthenticated operator may still configure a draft but cannot fire a
// bearer-less deploy. usePrivy() (inside AuthGate) THROWS outside <PrivyProvider>, so — mirroring the
// F-3 dashboard — the gate is only wired when Privy is CONFIGURED (NEXT_PUBLIC_PRIVY_APP_ID). In an
// unconfigured build no session is possible, so the deploy area fail-closes to an explicit prompt
// (never a blank, never an actionable deploy). Navigation targets the real instance page on success.
export default function AgentStudioPage() {
  const router = useRouter();
  // Navigate ONLY on a resolved, successful deploy — to the REAL instance page keyed by the
  // server-returned instance_id (F-3's route). On a fail-closed preflight this never fires.
  const onPin = (result: DeployAgentResult) => router.push(`/instances/${result.instance_id}`);
  const privyConfigured = Boolean(process.env.NEXT_PUBLIC_PRIVY_APP_ID);

  if (!privyConfigured) {
    // Fail-closed: sign-in is impossible in this build, so the deploy button is replaced by an
    // explicit prompt. AuthGate (and thus usePrivy) is NEVER rendered here.
    return (
      <StudioScreen
        onPin={onPin}
        deployGate={() => (
          <div className={authGateStyles.gate} data-testid="deploy-auth-required">
            <p className={authGateStyles.copy}>Sign-in is required to deploy, and is not configured in this build.</p>
          </div>
        )}
      />
    );
  }

  // Configured: gate the deploy button behind the real session. Signed out ⇒ AuthGate renders the
  // login affordance in place of the button (the button is absent from the DOM).
  return (
    <StudioScreen
      onPin={onPin}
      deployGate={(deployButton) => <AuthGate>{deployButton}</AuthGate>}
    />
  );
}
