'use client';
import { Suspense } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { usePrivy } from '@privy-io/react-auth';
import { CreateCompetitionScreen } from '@/components/screens/CreateCompetitionScreen';

// `connected` is DERIVED from the real Privy session (auth-contract@1), never a literal — the roster
// listing + the create/register/start POSTs only fire authenticated. usePrivy is read ONLY when Privy
// is configured (NEXT_PUBLIC_PRIVY_APP_ID), mirroring AuthProvider's own guard (reading it outside the
// provider throws); an unconfigured build fail-closes to the connect prompt (no session is possible).
function SessionCreate({ initialFixtureId }: { initialFixtureId?: number }) {
  const router = useRouter();
  const { ready, authenticated, login } = usePrivy();
  if (!ready) return null;
  return (
    <CreateCompetitionScreen
      initialFixtureId={initialFixtureId}
      connected={authenticated}
      onConnect={login}
      onLaunched={(competitionId) => router.push(`/arena/${competitionId}`)}
    />
  );
}

function CreateInner() {
  const router = useRouter();
  // Markets' "Launch a competition" links here with ?fixture=<id> → pre-scope the wizard.
  const fixtureParam = useSearchParams().get('fixture');
  const parsed = fixtureParam != null ? Number(fixtureParam) : NaN;
  const initialFixtureId = Number.isFinite(parsed) ? parsed : undefined;
  const privyConfigured = Boolean(process.env.NEXT_PUBLIC_PRIVY_APP_ID);

  // Unconfigured Privy → the wizard renders disconnected (roster shows "Connect wallet"); the launch
  // POSTs are unreachable without a session anyway. A launch still navigates to the arena on success.
  if (!privyConfigured) {
    return (
      <CreateCompetitionScreen
        initialFixtureId={initialFixtureId}
        connected={false}
        onLaunched={(competitionId) => router.push(`/arena/${competitionId}`)}
      />
    );
  }
  return <SessionCreate initialFixtureId={initialFixtureId} />;
}

export default function CreateCompetitionPage() {
  // useSearchParams requires a Suspense boundary for static generation (Next 15).
  return (
    <Suspense fallback={null}>
      <CreateInner />
    </Suspense>
  );
}
