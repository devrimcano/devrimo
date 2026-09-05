import type { NextConfig } from "next";
import { withPostHogConfig } from "@posthog/nextjs-config";

const nextConfig: NextConfig = {
  // Production stack traces are useless without maps to resolve them against.
  productionBrowserSourceMaps: true,
};

const personalApiKey = process.env.POSTHOG_PERSONAL_API_KEY;
const projectId = process.env.POSTHOG_PROJECT_ID;

/**
 * Source maps are uploaded to PostHog at build time and then deleted from the
 * output, so stack traces resolve to real source without shipping the maps to
 * every visitor.
 *
 * This needs a *personal* API key (`phx_...`), which is a build-time secret and
 * therefore deliberately not a `NEXT_PUBLIC_` variable. Without it — a local
 * `next build`, or a fork's CI — the plugin is skipped and the build succeeds
 * unchanged.
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
