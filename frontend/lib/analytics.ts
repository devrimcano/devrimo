/**
 * Starting and stopping measurement, on the visitor's word.
 *
 * The configuration below is unchanged from when it lived in
 * `instrumentation-client.ts`; what changed is that it no longer runs on its
 * own. `startAnalytics()` is called from exactly two places - once at startup
 * if a stored consent already says yes, and once from the banner the moment
 * someone says yes - and `stopAnalytics()` undoes it.
 *
 * See lib/consent.ts for why.
 */

import posthog from "posthog-js";
import { getPostHogHost, getPostHogKey, getTracingHostnames } from "@/lib/env";
import { clearMeasurementStorage, type ConsentCategories } from "@/lib/consent";
import { serviceProperties } from "@/lib/telemetry";

let started = false;

/**
 * Bring the SDK in line with what was actually agreed to.
 *
 * The two categories are not two vendors: measurement is what makes the SDK run
 * at all, and replay is a capability of the running SDK. So refusing
 * measurement means never starting, and refusing only replay means starting
 * without it - which is a live switch rather than an init flag, because someone
 * can turn replay off in settings while a recording is in progress.
 */
export function applyConsent(consent: ConsentCategories): void {
  if (!consent.measurement) {
    stopAnalytics();
    return;
  }
  startAnalytics(consent.replay);
  if (!started) return;
  try {
    // Also switched live, because settings can change the answer while the SDK
    // is already running and a recording is already in progress.
    if (consent.replay) posthog.startSessionRecording();
    else posthog.stopSessionRecording();
  } catch (error) {
    console.error("Applying session recording consent failed", error);
  }
}

/** Idempotent: consent can be re-granted, and a second init would double every event. */
export function startAnalytics(replay: boolean): void {
  if (started) return;
  const key = getPostHogKey();
  if (!key) {
    if (process.env.NODE_ENV !== "production") {
      console.error(
        "NEXT_PUBLIC_POSTHOG_KEY variable required by PostHog is missing or un-configured, " +
          "this causes events to be silently missed. This error stops appearing once " +
          "NEXT_PUBLIC_POSTHOG_KEY is configured",
      );
    }
    return;
  }

  // Next warns if client instrumentation takes longer than 16ms, and an
  // instrumentation failure must never be able to stop the app from becoming
  // interactive.
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

      // Session replay, only for someone who agreed to it specifically — it is
      // the one thing here that records the screen rather than counting an
      // event, so it is its own question and its own switch.
      //
      // `maskAllInputs` is what keeps the METU and Supabase password fields out
      // of recordings; it is PostHog's default, and stated here explicitly
      // because it is a requirement rather than a preference.
      disable_session_recording: !replay,
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
          for (const [property, value] of Object.entries(bag)) {
            if (typeof value !== "string") continue;
            if (!/(^\$|_)(current_url|pathname|referrer|url|host)$/.test(property) && !property.includes("_url")) {
              continue;
            }
            bag[property] = value.split(/[?#]/)[0];
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
    started = true;
  } catch (error) {
    console.error("PostHog initialisation failed", error);
  }
}

/**
 * Stop measuring, and remove what measuring left behind.
 *
 * The order is the whole of it, and the first version got it wrong. Clearing
 * the cookie and localStorage while the SDK is still alive clears nothing: it
 * keeps its state in memory and flushes it back on its next save, so a measured
 * withdrawal showed the sessionStorage keys gone and the cookie and
 * localStorage entry restored within two seconds - a right to withdraw that
 * undid itself.
 *
 * So persistence is switched to memory *first*. From that point the SDK has
 * nowhere on disk to write, including for the opt-out flag it would otherwise
 * persist, and the clear that follows is final.
 */
export function stopAnalytics(): void {
  try {
    if (started) {
      posthog.set_config({ persistence: "memory" });
      posthog.opt_out_capturing();
      posthog.reset(true);
    }
  } catch (error) {
    console.error("Stopping PostHog failed", error);
  } finally {
    started = false;
    clearMeasurementStorage();
  }
}
