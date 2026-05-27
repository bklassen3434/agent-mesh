import { Skeleton } from '@/components/skeleton';

// The briefing route can take 10-30s because the Personalizer LLM call
// dominates wall time. A skeleton beats a blank route.
export default function Loading() {
  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <Skeleton className="h-7 w-48" />
        <Skeleton className="h-4 w-96" />
        <Skeleton className="h-3 w-64" />
      </div>
      {[0, 1, 2].map((i) => (
        <section key={i} className="space-y-3">
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-24" />
          <Skeleton className="h-24" />
        </section>
      ))}
    </div>
  );
}
