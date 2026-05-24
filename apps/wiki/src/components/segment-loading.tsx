import { Skeleton } from '@/components/skeleton';

export function SegmentLoading() {
  return (
    <div className="space-y-4">
      <Skeleton className="h-7 w-40" />
      <Skeleton className="h-4 w-72" />
      <Skeleton className="h-64" />
    </div>
  );
}
