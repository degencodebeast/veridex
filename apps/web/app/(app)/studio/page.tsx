'use client';
import { useRouter } from 'next/navigation';
import { StudioScreen } from '@/components/screens/StudioScreen';

export default function AgentStudioPage() {
  const router = useRouter();
  return <StudioScreen onPin={() => router.push('/dashboard')} />;
}
