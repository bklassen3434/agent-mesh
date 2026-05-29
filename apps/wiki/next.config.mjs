/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  reactStrictMode: true,
  // Phase 9 moved the knowledge sections under /knowledge/*. Redirect the old
  // top-level paths (and any nested detail/timeline routes) so existing links
  // and bookmarks keep working.
  async redirects() {
    const sections = ['beliefs', 'entities', 'claims', 'sources'];
    return sections.flatMap((s) => [
      { source: `/${s}`, destination: `/knowledge/${s}`, permanent: false },
      { source: `/${s}/:path*`, destination: `/knowledge/${s}/:path*`, permanent: false },
    ]);
  },
};

export default nextConfig;
