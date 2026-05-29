import { NavBar } from '@/components/nav-bar';

// Status lives on the API service, not the wiki — link out so it visually
// reads as an admin surface, not another wiki tab.
const statusHref =
  (process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000') + '/status';

export function Nav() {
  return <NavBar statusHref={statusHref} />;
}
