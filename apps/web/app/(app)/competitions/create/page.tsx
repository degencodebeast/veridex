'use client';
import { Suspense } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { CreateCompetitionScreen } from '@/components/screens/CreateCompetitionScreen';

function CreateInner() {
  const router = useRouter();
  // Markets' "Launch a competition" links here with ?fixture=<id> → pre-scope the wizard.
  const fixtureParam = useSearchParams().get('fixture');
  const parsed = fixtureParam != null ? Number(fixtureParam) : NaN;
  const initialFixtureId = Number.isFinite(parsed) ? parsed : undefined;
  return <CreateCompetitionScreen initialFixtureId={initialFixtureId} onCommit={() => router.push('/arena/new')} />;
}

export default function CreateCompetitionPage() {
  // useSearchParams requires a Suspense boundary for static generation (Next 15).
  return (
    <Suspense fallback={null}>
      <CreateInner />
    </Suspense>
  );
}
