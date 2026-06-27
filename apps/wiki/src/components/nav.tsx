import { NavBar } from '@/components/nav-bar';
import { api } from '@/lib/api';
import { adminLoginConfigured, getField, getRole } from '@/lib/auth-server';

// Status lives on the API service, not the wiki — link out so it visually
// reads as an admin surface, not another wiki tab.
const statusHref =
  (process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000') + '/status';

export async function Nav() {
  const [role, field] = await Promise.all([getRole(), getField()]);

  // Topics = fields. Both roles see the list (to switch); only admins create.
  let topics: { slug: string; name: string }[] = [];
  try {
    const fields = await api.listFields(true);
    topics = fields.map((f) => ({ slug: f.slug, name: f.name }));
  } catch {
    topics = [];
  }

  return (
    <NavBar
      statusHref={statusHref}
      role={role}
      field={field}
      topics={topics}
      loginConfigured={adminLoginConfigured()}
    />
  );
}
