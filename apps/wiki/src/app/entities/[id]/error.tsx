'use client';

import { SegmentError } from '@/components/segment-error';

export default function Error({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return <SegmentError error={error} reset={reset} />;
}
