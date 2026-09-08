import type { NextConfig } from "next";
import { withPostHogConfig } from "@posthog/nextjs-config";

const personalApiKey = process.env.POSTHOG_PERSONAL_API_KEY?.trim();
const projectId = process.env.POSTHOG_PROJECT_ID?.trim();
const nextConfig: NextConfig = {
  // Generate browser maps only when the uploader can remove them after upload.
  productionBrowserSourceMaps: Boolean(personalApiKey && projectId),
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
