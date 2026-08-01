/** @type {import('next').NextConfig} */

const nextConfig = {
  output: "standalone",
  images: {
    // All present image sources are versioned local brand assets.  Serving
    // them directly avoids a native sharp runtime dependency in the small
    // standalone artifact and keeps image delivery deterministic.
    unoptimized: true,
  },
  experimental: {
    instrumentationHook: true,
  },
};

export default nextConfig;
