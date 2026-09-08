/**
 * Client-side PostHog initialisation.
 *
 * Next.js runs this once, before the app renders. That timing is the point:
 * autocapture, session replay and exception capture all need the SDK live
 * before the first user interaction, which the previous lazy-init-on-first-
 * event approach could never provide.
 *
 * A missing key must not break the app, but must not be invisible either —
 * with no key configured this logs once in development and then does nothing.
 */

import posthog from "posthog-js";
import { getPostHogHost, getPostHogKey, getTracingHostnames } from "@/lib/env";
import { serviceProperties } from "@/lib/telemetry";

const key = getPostHogKey();

if (key) {
  // Next warns if this file takes longer than 16ms, and an instrumentation
  // failure must never be able to stop the app from becoming interactive.
  try {
    posthog.init(key, {
      api_host: getPostHogHost(),
      defaults: "2026-05-30",

      // Events from signed-out visitors stay anonymous; a person profile is
      // created only once identify() runs after sign-in.
      person_profiles: "identified_only",

      // Web analytics, heatmaps and web vitals.
      autocapture: true,
      capture_pageview: true,
      capture_pageleave: true,
      capture_performance: true,
      enable_heatmaps: true,

      // Error tracking for anything that escapes an error boundary, plus
      // unhandled promise rejections — of which this app had zero coverage.
      capture_exceptions: true,

      // Session replay. `maskAllInputs` is what keeps the METU and Supabase
      // password fields out of recordings; it is PostHog's default, and stated
      // here explicitly because it is a requirement rather than a preference.
      disable_session_recording: false,
      session_recording: {
        maskAllInputs: true,
        maskTextSelector: "[data-ph-mask]",
        recordCrossOriginIframes: false,
      },

      disable_surveys: false,

      // Puts X-POSTHOG-DISTINCT-ID / X-POSTHOG-SESSION-ID on same-origin fetches
      // to `/api/*`, which the route handlers forward to the broker. Without it
      // the backend's LLM traces cannot be linked to this session's replay.
      tracing_headers: getTracingHostnames(),

      // The auth flow puts `?next=` and `?error=` in the URL, and PostHog
      // records the entry URL in several places besides `$current_url`
      // (`$initial_current_url`, `$session_entry_url`, ...). Stripping the
      // query from every URL-shaped property covers all of them, including
      // ones added by future SDK versions.
      //
      // `before_send` rather than `sanitize_properties`: the latter is
      // deprecated in this SDK version, and only this one sees `$set_once`.
      before_send: (event) => {
        if (!event) return event;
        for (const bag of [event.properties, event.$set, event.$set_once]) {
          if (!bag) continue;
          for (const [key, value] of Object.entries(bag)) {
            if (typeof value !== "string") continue;
            if (!/(^\$|_)(current_url|pathname|referrer|url|host)$/.test(key) && !key.includes("_url")) {
              continue;
            }
            bag[key] = value.split(/[?#]/)[0];
          }
        }
        return event;
      },
    });

    // Service, environment and release on every browser event, matching the
    // labels the broker and the knowledge worker report. Without the release a
    // spike in browser exceptions cannot be attributed to a deploy, and the
    // source maps uploaded at build time are keyed by the same commit.
    posthog.register(serviceProperties("devrimo-web"));
  } catch (error) {
    console.error("PostHog initialisation failed", error);
  }
} else if (process.env.NODE_ENV !== "production") {
  console.error(
    "NEXT_PUBLIC_POSTHOG_KEY variable required by PostHog is missing or un-configured, " +
      "this causes events to be silently missed. This error stops appearing once " +
      "NEXT_PUBLIC_POSTHOG_KEY is configured",
  );
}
