import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { adminLoginConfigured } from '@/lib/auth-server';

import { LoginForm } from './login-form';

export const dynamic = 'force-dynamic';

export default function LoginPage() {
  const configured = adminLoginConfigured();
  return (
    <div className="mx-auto max-w-md">
      <Card>
        <CardHeader>
          <CardTitle>Admin sign in</CardTitle>
        </CardHeader>
        <CardContent>
          {configured ? (
            <LoginForm />
          ) : (
            <p className="text-sm text-muted-foreground">
              Admin login isn&apos;t configured on this deployment. Set
              <code className="mx-1 rounded bg-muted px-1">MESH_ADMIN_PASSWORD</code>
              to enable it.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
