import posthog from "posthog-js";

export type ScheduleStep = "details" | "courses" | "timetable";
export type ScheduleSource =
  | "context"
  | "curriculum"
  | "catalog"
  | "sections"
  | "constraints"
  | "saved"
  | "cache"
  | "unknown";
export type ScheduleOutcome =
  | "success"
  | "error"
  | "conflict"
  | "cancelled"
  | "unavailable"
  | "incomplete";
export type ScheduleFlow = "automatic" | "manual" | "assistant" | "what_if";
export type ScheduleFormat = "ics" | "csv" | "wallpaper";

export type ScheduleGenerationTerminal = {
  flowId: string;
  flow: ScheduleFlow;
  outcome: ScheduleOutcome;
  durationMs: number;
  requestedCount: number;
  returnedCount: number;
  requestId?: string | null;
};

export type ScheduleExportTerminal = {
  flowId: string;
  format: ScheduleFormat;
  outcome: ScheduleOutcome;
  status: number | null;
  requestId?: string | null;
};

/**
 * A schedule flow id is a random correlation id, never a course or account
 * identifier. It lets one terminal event win when retries/renders race without
 * making the event a high-cardinality copy of planner state.
 */
export function newScheduleFlowId(): string {
  try {
    const random = globalThis.crypto?.randomUUID?.();
    if (random) return random;
  } catch {
    // Browser telemetry must also work in older or restricted preview contexts.
  }
  return `schedule-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

const terminalEvents = new Set<string>();
type ScheduleEventPayload = Record<string, unknown>;
type ScheduleEventSink = (event: ScheduleEventName, properties: ScheduleEventPayload) => void;

const SCHEDULE_STEPS = ["details", "courses", "timetable"] as const;
const SCHEDULE_SOURCES = ["context", "curriculum", "catalog", "sections", "constraints", "saved", "cache", "unknown"] as const;
const SCHEDULE_OUTCOMES = ["success", "error", "conflict", "cancelled", "unavailable", "incomplete"] as const;
const SCHEDULE_FLOWS = ["automatic", "manual", "assistant", "what_if"] as const;
const SCHEDULE_FORMATS = ["ics", "csv", "wallpaper"] as const;

function isOneOf<T extends string>(value: unknown, allowed: readonly T[]): value is T {
  return typeof value === "string" && (allowed as readonly string[]).includes(value);
}

let eventSink: ScheduleEventSink = (event, properties) => {
  if (typeof window === "undefined" || !posthog.__loaded) return;
  posthog.capture(event, properties);
};

function boundedFlowId(flowId: string | undefined): string {
  const value = typeof flowId === "string" ? flowId.trim() : "";
  if (/^(?:schedule-[a-z0-9-]{1,90}|[0-9a-f]{8}-[0-9a-f-]{27,}|unknown)$/i.test(value)) return value.slice(0, 96);
  return "unknown";
}

function boundedRequestId(requestId: string | null | undefined): string | null {
  if (typeof requestId !== "string") return null;
  const value = requestId.trim();
  // Request ids are opaque correlation values. Keep a bounded printable value;
  // never accept a URL, route or arbitrary error text as a telemetry id.
  return /^[A-Za-z0-9._:-]{1,128}$/.test(value) ? value : null;
}

function boundedCount(value: number): number {
  return Number.isFinite(value) ? Math.max(0, Math.min(500, Math.trunc(value))) : 0;
}

function boundedDuration(value: number): number {
  return Number.isFinite(value) ? Math.max(0, Math.min(3_600_000, Math.round(value))) : 0;
}

function boundedStatus(value: number | null): number | null {
  if (value === null || !Number.isFinite(value)) return null;
  const status = Math.trunc(value);
  return status >= 100 && status <= 599 ? status : null;
}

type ScheduleEventName =
  | "schedule_step_viewed"
  | "schedule_step_completed"
  | "schedule_source_load_terminal"
  | "schedule_generation_terminal"
  | "schedule_save_conflict"
  | "schedule_export_terminal";

function captureOnce(key: string, event: ScheduleEventName, properties: Record<string, unknown>): void {
  if (terminalEvents.has(key)) return;
  terminalEvents.add(key);
  try {
    eventSink(event, properties);
  } catch {
    // Analytics is optional and must never break a planner action. Marking the
    // key before capture also prevents a throwing SDK from causing a retry
    // storm in a terminal callback.
  }
}

export function scheduleStepViewed(step: ScheduleStep, flowId?: string, requestId?: string | null): void {
  if (!isOneOf(step, SCHEDULE_STEPS)) return;
  const id = boundedFlowId(flowId);
  try {
    eventSink("schedule_step_viewed", {
      step,
      flow_id: id,
      request_id: boundedRequestId(requestId),
    });
  } catch {
    // Best effort by design.
  }
}

export function scheduleStepCompleted(
  step: ScheduleStep,
  outcome: ScheduleOutcome,
  flowId?: string,
  requestId?: string | null,
): void {
  if (!isOneOf(step, SCHEDULE_STEPS) || !isOneOf(outcome, SCHEDULE_OUTCOMES)) return;
  const id = boundedFlowId(flowId);
  captureOnce(`step:${id}:${step}`, "schedule_step_completed", {
    step,
    outcome,
    flow_id: id,
    request_id: boundedRequestId(requestId),
  });
}

export function scheduleSourceLoadTerminal(
  source: ScheduleSource,
  outcome: ScheduleOutcome,
  flowId?: string,
  requestId?: string | null,
): void {
  if (!isOneOf(source, SCHEDULE_SOURCES) || !isOneOf(outcome, SCHEDULE_OUTCOMES)) return;
  const id = boundedFlowId(flowId);
  captureOnce(`source:${id}:${source}`, "schedule_source_load_terminal", {
    source,
    outcome,
    flow_id: id,
    request_id: boundedRequestId(requestId),
  });
}

export function scheduleGenerationTerminal(input: ScheduleGenerationTerminal): void {
  if (!isOneOf(input.flow, SCHEDULE_FLOWS) || !isOneOf(input.outcome, SCHEDULE_OUTCOMES)) return;
  const flowId = boundedFlowId(input.flowId);
  captureOnce(`generation:${flowId}`, "schedule_generation_terminal", {
    flow_id: flowId,
    flow: input.flow,
    outcome: input.outcome,
    duration_ms: boundedDuration(input.durationMs),
    requested_count: boundedCount(input.requestedCount),
    returned_count: boundedCount(input.returnedCount),
    request_id: boundedRequestId(input.requestId),
  });
}

export function scheduleSaveConflict(flowId?: string, requestId?: string | null): void {
  const id = boundedFlowId(flowId);
  captureOnce(`save-conflict:${id}`, "schedule_save_conflict", {
    flow_id: id,
    request_id: boundedRequestId(requestId),
  });
}

export function scheduleExportTerminal(input: ScheduleExportTerminal): void {
  if (!isOneOf(input.format, SCHEDULE_FORMATS) || !isOneOf(input.outcome, SCHEDULE_OUTCOMES)) return;
  const flowId = boundedFlowId(input.flowId);
  captureOnce(`export:${flowId}:${input.format}`, "schedule_export_terminal", {
    flow_id: flowId,
    format: input.format,
    outcome: input.outcome,
    status: boundedStatus(input.status),
    request_id: boundedRequestId(input.requestId),
  });
}

/** Test-only reset for the standalone offline contract tests. */
export function resetScheduleTelemetryForTests(): void {
  terminalEvents.clear();
}

/** Internal offline-test seam; application call sites should use the typed helpers. */
export function setScheduleTelemetrySinkForTests(sink: ScheduleEventSink): () => void {
  const previous = eventSink;
  eventSink = sink;
  return () => {
    eventSink = previous;
  };
}
