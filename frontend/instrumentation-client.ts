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

const ACADEMIC_CONTENT_KEY = /(?:prompt|completion|messages?|content|transcript|course|section|surname|grade|student|academic|context|tool[_-]?result|detail|error(?:s|[_-]?(?:message|detail|body))?|instructions?|answer|response|input|output|\$ai_error(?:_|$))/i;
const USAGE_METRIC_KEY = /(^|_)(tokens?|latency|duration|count|status|price|cost|seconds?|milliseconds?)($|_)/i;
const ACADEMIC_REDACTED = "[academic content redacted]";
const SAFE_TYPE_KEYS = new Set(["error_type", "exception_type", "$ai_error_type"]);
const SAFE_TYPE_VALUE = /^[A-Za-z][A-Za-z0-9_.:-]{0,127}$/;

function safeTypeValue(keyName: string | undefined, value: unknown): boolean {
  return !keyName || !SAFE_TYPE_KEYS.has(keyName.toLowerCase()) || value == null ||
    (typeof value === "string" && SAFE_TYPE_VALUE.test(value.trim()));
}

function redactAcademicTelemetry(value: unknown, keyName?: string, depth = 0): unknown {
  if (depth > 12) return ACADEMIC_REDACTED;
  if (typeof value === "string") {
    if (keyName && SAFE_TYPE_KEYS.has(keyName.toLowerCase())) {
      return safeTypeValue(keyName, value) ? value : ACADEMIC_REDACTED;
    }
    return keyName && ACADEMIC_CONTENT_KEY.test(keyName) ? ACADEMIC_REDACTED : value;
  }
  if (Array.isArray(value)) return value.map((item) => redactAcademicTelemetry(item, keyName, depth + 1));
  if (!value || typeof value !== "object") return value;
  const output: Record<string, unknown> = {};
  for (const [childKey, childValue] of Object.entries(value)) {
    const safeType = safeTypeValue(childKey, childValue);
    if (
      (ACADEMIC_CONTENT_KEY.test(childKey) ||
        ["$exception_list", "exception", "stack", "error", "error_message", "error_detail", "error_body"].includes(childKey)) &&
      !USAGE_METRIC_KEY.test(childKey) &&
      !safeType
    ) {
      output[childKey] = ACADEMIC_REDACTED;
    } else {
      output[childKey] = redactAcademicTelemetry(childValue, childKey, depth + 1);
    }
  }
  return output;
}

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
        // Keep PostHog's built-in privacy classes when adding the academic
        // selector. A custom block/ignore class would otherwise replace the
        // SDK defaults and make `ph-no-capture` ineffective.
        blockClass: "ph-no-capture",
        ignoreClass: "ph-ignore-input",
        maskTextClass: "ph-mask",
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
        // AI traces, exception properties and explicit product events can all
        // pass through this hook. Keep aggregate measurements and correlation
        // ids, but remove academic content even when a future call site forgets
        // to use the safe product-event helper.
        for (const bag of [event.properties, event.$set, event.$set_once]) {
          if (!bag) continue;
          const safe = redactAcademicTelemetry(bag);
          if (safe && typeof safe === "object" && !Array.isArray(safe)) {
            Object.assign(bag, safe);
          }
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
