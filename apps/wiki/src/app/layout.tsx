import './globals.css';
import type { Metadata } from 'next';
import type { ReactNode } from 'react';

import { Nav } from '@/components/nav';
import { api } from '@/lib/api';
import { formatDateTime } from '@/lib/format';

export const metadata: Metadata = {
  title: 'Agent Mesh',
  description: 'A persistent multi-agent system tracking AI/robotics research.',
};

async function getLastRunTime(): Promise<string | null> {
  try {
    const stats = await api.stats();
    return stats.last_pipeline_run_at ?? null;
  } catch {
    return null;
  }
}

export default async function RootLayout({ children }: { children: ReactNode }) {
  const lastRun = await getLastRunTime();
  return (
    <html lang="en">
      <body className="min-h-full">
        <Nav />
        <div className="mx-auto max-w-6xl px-6 py-8">{children}</div>
        <footer className="mt-12 border-t border-border bg-card">
          <div className="mx-auto max-w-6xl px-6 py-4 text-xs text-muted-foreground">
            Data current as of {formatDateTime(lastRun)} · Agent Mesh v0.4.0
          </div>
        </footer>
      </body>
    </html>
  );
}
