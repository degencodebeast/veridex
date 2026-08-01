'use client';
import { Suspense } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { usePrivy } from '@privy-io/react-auth';
import { CreateCompetitionScreen } from '@/components/screens/CreateCompetitionScreen';

// `connected` is DERIVED from the real Privy session (auth-contract@1), never a literal — the roster
// listing + the create/register/start POSTs only fire authenticated. usePrivy is read ONLY when Privy
// is configured (NEXT_PUBLIC_PRIVY_APP_ID), mirroring AuthProvider's own guard (reading it outside the
// provider throws); an unconfigured build fail-closes to the connect prompt (no session is possible).
function SessionCreate({ initialFixtureId, packId }: { initialFixtureId?: number; packId?: string }) {
  const router = useRouter();
  const { ready, authenticated, login } = usePrivy();
  if (!ready) return null;
  return (
    <CreateCompetitionScreen
      initialFixtureId={initialFixtureId}
      packId={packId}
      connected={authenticated}
      onConnect={login}
      onLaunched={(competitionId) => router.push(`/arena/${competitionId}`)}
    />
  );
}

function CreateInner() {
  const router = useRouter();
  const sp = useSearchParams();
  // The Markets Replay Library links here with ?pack_id=<id>&fixture_id=<id> (the authoritative catalog
  // identity, spec §5.2); the legacy per-fixture Markets launch still uses ?fixture=<id>. Parse the
  // fixture id (new param first, legacy fallback) and the pack id ONCE, validated to finite / non-empty,
  // and thread BOTH into whichever auth branch renders — so the create payload carries the catalog
  // identity, not just free-form market_scope text (a label-only prefill loses it the moment a second
  // admitted pack appears).
  const fixtureParam = sp.get('fixture_id') ?? sp.get('fixture');
  const parsed = fixtureParam != null ? Number(fixtureParam) : NaN;
  const initialFixtureId = Number.isFinite(parsed) ? parsed : undefined;
  const packParam = sp.get('pack_id');
  const initialPackId = packParam && packParam.trim() ? packParam : undefined;
  const privyConfigured = Boolean(process.env.NEXT_PUBLIC_PRIVY_APP_ID);

  // Unconfigured Privy → the wizard renders disconnected (roster shows "Connect wallet"); the launch
  // POSTs are unreachable without a session anyway. A launch still navigates to the arena on success.
  if (!privyConfigured) {
    return (
      <CreateCompetitionScreen
        initialFixtureId={initialFixtureId}
        packId={initialPackId}
        connected={false}
        onLaunched={(competitionId) => router.push(`/arena/${competitionId}`)}
      />
    );
  }
  return <SessionCreate initialFixtureId={initialFixtureId} packId={initialPackId} />;
}

export default function CreateCompetitionPage() {
  // useSearchParams requires a Suspense boundary for static generation (Next 15).
  return (
    <Suspense fallback={null}>
      <CreateInner />
    </Suspense>
  );
}
