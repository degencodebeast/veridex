'use client';
import { useRouter } from 'next/navigation';
import { CreateCompetitionScreen } from '@/components/screens/CreateCompetitionScreen';

export default function CreateCompetitionPage() {
  const router = useRouter();
  return <CreateCompetitionScreen onCommit={() => router.push('/arena/new')} />;
}
