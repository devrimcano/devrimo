"use client";

import posthog from "posthog-js";
import { PostHogProvider } from "posthog-js/react";
import { type ReactNode } from "react";
import { ApiError, requestIdOf } from "@/lib/api/errors";
import { OUTCOME_EXPECTED_FAILURE, OUTCOME_UNEXPECTED_FAILURE } from "@/lib/telemetry";

/**
 * The product event contract.
 *
 * This map is the single source of truth for event names *and* their property
 * shapes — adding a property here and forgetting it at a call site is a
 * compile error, which is the only reliable way to keep an event schema honest
 * across a codebase.
 *
 * Initialisation lives in `instrumentation-client.ts`, not here: pageviews,
 * autocapture, session replay and exception capture all need the SDK live
 * before the first render.
 */
type ProductEventProperties = {
  // --- chat ---------------------------------------------------------------
  chat_new_clicked: Record<string, never>;
  chat_opened: { source: "history" };
  chat_delete_requested: { was_active: boolean };
  chat_deleted: { was_active: boolean };
  chat_deleted_all: { count: number };
  // `request_id` is the correlation id this turn was sent with. The broker
  // tags its own events, logs and issues with the same value, so a student's
  // failed turn is one query rather than a timestamp comparison.
  chat_message_sent: {
    conversation_type: "new" | "existing";
    message_position: number;
    text_length: number;
    attachment_count: number;
    request_id: string;
  };
  chat_response_completed: { duration_seconds: number; request_id: string | null };
  chat_response_error: {
    category: "busy" | "network" | "other";
    status: number | null;
    error_code: string | null;
    duration_seconds: number | null;
    request_id: string | null;
  };
  // Tool activity the broker streams as `devrimo` extension chunks. The
  // frontend received these already and threw them away.
  agent_tool_call: { tool: string | null; server: string | null; status: "started" | "completed" | "error" };
  // The denominator for the approve/reject funnel: without it, a confirmation
  // the student simply abandoned is indistinguishable from one never shown.
  chat_confirmation_shown: { tool: string | null };
  // One terminal event per confirmation the student acted on. `result` was
  // added because the event was previously emitted only on success: an
  // approval that failed to reach the broker counted as neither approved nor
  // rejected, and simply left the funnel.
  agent_action_confirmation: {
    approved: boolean;
    tool: string;
    result: "completed" | "failed";
    awaiting_confirmation: boolean;
  };

  // --- agent + campus -----------------------------------------------------
  campus_connection_saved: { source: "onboarding" | "settings"; result: "success" | "error"; verification_skipped: boolean };
  campus_tools_changed: { source: "onboarding" | "settings"; tool_count: number; result: "success" | "error" };
  campus_disconnected: { result: "success" | "error" };

  // --- onboarding + preferences ------------------------------------------
  onboarding_step_viewed: { step: "welcome" | "connect" | "privacy" | "ready" };
  onboarding_connection_result: { result: "success" | "error"; verification_skipped: boolean };
  onboarding_tool_selection_saved: { tool_count: number; result: "success" | "error" };
  onboarding_finished: { path: "completed" | "skipped" };
  theme_changed: { theme: "light" | "dark" };
  language_changed: { locale: "tr" | "en" };
  settings_opened: Record<string, never>;

  // --- auth ---------------------------------------------------------------
  // Sign-in and sign-out were instrumented for exceptions only, so a student
  // who could not get in produced an issue and no funnel.
  auth_submitted: { mode: "sign-in" | "sign-up" };
  auth_result: { mode: "sign-in" | "sign-up"; result: "success" | "error"; reason: string | null };
  auth_signed_out: { result: "success" | "error" };

  // --- scheduling ---------------------------------------------------------
  // Schedule telemetry is intentionally aggregate-only. Course identifiers,
  // names, sections, academic context and upstream response bodies never
  // belong in browser analytics.
  schedule_step_viewed: {
    step: "details" | "courses" | "timetable";
    flow_id: string;
    request_id: string | null;
  };
  schedule_step_completed: {
    step: "details" | "courses" | "timetable";
    outcome: "success" | "error" | "conflict" | "cancelled" | "unavailable" | "incomplete";
    flow_id: string;
    request_id: string | null;
  };
  schedule_source_load_terminal: {
    source: "context" | "curriculum" | "catalog" | "sections" | "constraints" | "saved" | "cache" | "unknown";
    outcome: "success" | "error" | "conflict" | "cancelled" | "unavailable" | "incomplete";
    flow_id: string;
    request_id: string | null;
  };
  schedule_generation_terminal: {
    flow_id: string;
    flow: "automatic" | "manual" | "assistant" | "what_if";
    outcome: "success" | "error" | "conflict" | "cancelled" | "unavailable" | "incomplete";
    duration_ms: number;
    requested_count: number;
    returned_count: number;
    request_id: string | null;
  };
  schedule_save_conflict: {
    flow_id: string;
    request_id: string | null;
  };
  schedule_export_terminal: {
    flow_id: string;
    format: "ics" | "csv" | "wallpaper";
    outcome: "success" | "error" | "conflict" | "cancelled" | "unavailable" | "incomplete";
    status: number | null;
    request_id: string | null;
  };
  /** @deprecated Kept until existing planner call sites migrate to terminals. */
  schedule_plan_completed: {
    result: "success" | "error";
    requested_courses: number;
    returned_courses: number;
    warnings: number;
    duration_seconds: number;
  };

  // --- data fetching ------------------------------------------------------
  // Every TanStack query and mutation failure, reported centrally rather than
  // once per component that remembered to.
  admin_section_viewed: { section: "researchers" };
  data_request_failed: {
    operation: string;
    kind: "query" | "mutation";
    status: number | null;
    outcome: typeof OUTCOME_EXPECTED_FAILURE | typeof OUTCOME_UNEXPECTED_FAILURE;
    request_id: string | null;
  };
};

function isReady() {
  return typeof window !== "undefined" && posthog.__loaded;
}

export function captureProductEvent<EventName extends keyof ProductEventProperties>(
  event: EventName,
  properties: ProductEventProperties[EventName],
) {
  if (!isReady()) return;
  try {
    posthog.capture(event, properties);
  } catch {
    // Analytics is optional. A blocked queue, browser extension or SDK update
    // must never break the product action that emitted the event.
  }
}

// One failure should be one issue. A mutation that fails reaches both the
// component's own catch block and the central mutation-cache handler, and both
// are worth keeping — the component knows which product action was being
// attempted, the cache handler catches everything nobody remembered to wrap.
// Whichever runs first wins.
const reportedErrors = new WeakSet<object>();

/**
 * Report a handled error that would otherwise only become a toast.
 *
 * The app catches almost everything and shows a `sonner` message, which is
 * right for the student and useless for us — these calls are what make those
 * failures countable.
 *
 * Returns whether this call is the one that reported it.
 */
export function captureError(error: unknown, context: Record<string, unknown> = {}) {
  if (!isReady()) return false;
  if (typeof error === "object" && error !== null) {
    if (reportedErrors.has(error)) return false;
    reportedErrors.add(error);
  }
  const exception = new Error("client_error");
  // Keep a stable exception class for grouping while excluding an API/body or
  // chat message that may contain academic content.
  exception.name = error instanceof Error && /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/.test(error.name)
    ? error.name
    : "ClientError";
  const safeContext: Record<string, unknown> = {
    // Ties a browser issue to the proxy log and the broker issue for the same
    // failure. Present whenever the failure came back from our own API.
    request_id: requestIdOf(error),
  };
  for (const key of ["source", "mode", "error_code", "operation", "kind", "status"]) {
    const value = context[key];
    if (typeof value === "string" || typeof value === "number") safeContext[key] = value;
  }
  try {
    posthog.captureException(exception, safeContext);
    return true;
  } catch {
    return false;
  }
}

/**
 * Report a failed data fetch, once, from wherever it was noticed first.
 *
 * A 4xx is the product saying no — an expired session, a conflict — and is
 * recorded as an event only. Anything else is a defect or a dependency that
 * broke, and additionally becomes an issue.
 */
export function captureRequestFailure(
  error: unknown,
  { operation, kind }: { operation: string; kind: "query" | "mutation" },
) {
  const status = error instanceof ApiError ? error.status : null;
  const expected = status !== null && status >= 400 && status < 500;

  captureProductEvent("data_request_failed", {
    operation,
    kind,
    status,
    outcome: expected ? OUTCOME_EXPECTED_FAILURE : OUTCOME_UNEXPECTED_FAILURE,
    request_id: requestIdOf(error),
  });

  if (!expected) captureError(error, { source: "data_request", operation, kind });
}

/**
 * Link this browser to a student.
 *
 * The verified Supabase user id links events to the right person. Academic
 * profile fields, campus usernames and email are deliberately excluded.
 */
export function identifyStudent(userId: string) {
  if (!isReady() || !userId) return;
  // The verified Supabase subject is an opaque correlation id. Academic
  // profile fields and campus usernames are deliberately never copied into
  // the analytics person profile.
  try {
    posthog.identify(userId);
  } catch {
    // A telemetry SDK failure must not block auth or page rendering.
  }
}

/**
 * Break the link on sign-out.
 *
 * Without this, the next person to use a shared machine — a lab computer, a
 * friend's laptop — inherits the previous student's identity and their events
 * land on the wrong person.
 */
export function resetStudent() {
  if (!isReady()) return;
  try {
    posthog.reset();
  } catch {
    // Sign-out must complete even if the optional analytics sink is broken.
  }
}

export function PostHogAnalytics({ children }: { children: ReactNode }) {
  return <PostHogProvider client={posthog}>{children}</PostHogProvider>;
}
