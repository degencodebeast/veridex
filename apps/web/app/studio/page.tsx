'use client';
import { useRouter } from 'next/navigation';
import { AgentStudioScreen } from '@/components/screens/AgentStudioScreen';

export default function AgentStudioPage() {
  const router = useRouter();
  return <AgentStudioScreen onPin={() => router.push('/dashboard')} />;
}
