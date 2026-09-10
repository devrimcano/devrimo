import type { NextConfig } from "next";
import { withPostHogConfig } from "@posthog/nextjs-config";

const personalApiKey = process.env.POSTHOG_PERSONAL_API_KEY?.trim();
const projectId = process.env.POSTHOG_PROJECT_ID?.trim();
/**
 * Headers this application shipped without, measured on the live domain.
 *
 * The one that was already exploitable: with no frame protection, /schedule
 * renders inside an iframe carrying the visitor's session - demonstrated, not
 * assumed - so a page anywhere can lay itself over "Tüm sohbetleri sil",
 * "Programı temizle" or the admin panel's publish and rollback and collect the
 * click. The session cookie is script-readable, as @supabase/ssr needs it to
 * be, which is exactly why the cheap defences around it are worth having.
 *
 * A full Content-Security-Policy is not here: this page loads PostHog and
 * Supabase and Next's own inline bootstrap, and a policy written blind breaks
 * a working site. frame-ancestors is the part of CSP that cannot be expressed
 * by any other header, so that part ships now and the rest is its own job.
 */
const securityHeaders = [
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Content-Security-Policy", value: "frame-ancestors 'none'" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  // Send the origin to other sites, never the path: a course code, a "?next="
  // or a session id has no business in someone else's logs.
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  // Nothing here asks for a camera, a microphone or a location.
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), browsing-topics=()" },
  // Six months, no preload: reversible if the domain ever needs plain HTTP,
  // and browsers ignore it there anyway.
  { key: "Strict-Transport-Security", value: "max-age=15552000; includeSubDomains" },
];

const nextConfig: NextConfig = {
  // Generate browser maps only when the uploader can remove them after upload.
  productionBrowserSourceMaps: Boolean(personalApiKey && projectId),
  // The version banner is a free hint to anyone scanning for a known Next bug.
  poweredByHeader: false,
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
};

/**
 * Source maps are uploaded to PostHog at build time and then deleted from the
 * output, so stack traces resolve to real source without shipping the maps to
 * every visitor.
 *
 * This needs a *personal* API key (`phx_...`), which is a build-time secret and
 * therefore deliberately not a `NEXT_PUBLIC_` variable. Without it — a local
 * `next build`, or a fork's CI — the plugin is skipped and the build succeeds
 * without generating public browser source maps.
 */
export default personalApiKey && projectId
  ? withPostHogConfig(nextConfig, {
      personalApiKey,
      projectId,
      host: process.env.NEXT_PUBLIC_POSTHOG_HOST ?? "https://eu.i.posthog.com",
      sourcemaps: {
        enabled: true,
        deleteAfterUpload: true,
        // The same commit `scripts/deploy-vps.sh` exports as
        // NEXT_PUBLIC_RELEASE, so a browser exception and the source map that
        // resolves its stack always name the same build.
        releaseVersion:
          process.env.VERCEL_GIT_COMMIT_SHA ??
          process.env.GIT_COMMIT_SHA ??
          process.env.NEXT_PUBLIC_RELEASE,
      },
    })
  : nextConfig;
