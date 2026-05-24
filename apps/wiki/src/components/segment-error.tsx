'use client';

import { useEffect } from 'react';

import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

export function SegmentError({
  error,
  reset,
  title = 'Could not load this page',
}: {
  error: Error & { digest?: string };
  reset: () => void;
  title?: string;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <p className="text-muted-foreground">{error.message || 'Unexpected error reaching the API.'}</p>
        <Button onClick={reset} variant="outline">Try again</Button>
      </CardContent>
    </Card>
  );
}
