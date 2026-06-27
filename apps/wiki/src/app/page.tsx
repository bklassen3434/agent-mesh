import { ChatPanel } from '@/components/chat-panel';
import { getField, getRole } from '@/lib/auth-server';

export const dynamic = 'force-dynamic';

// The front page is the chatbot — a grounded Q&A over the selected topic's
// knowledge base. Admins reach the dashboards (knowledge, agents, pipelines)
// from the nav; beta visitors get the chat, graph, and connectors.
export default async function HomePage() {
  const [field, role] = await Promise.all([getField(), getRole()]);
  return <ChatPanel field={field} role={role} />;
}
