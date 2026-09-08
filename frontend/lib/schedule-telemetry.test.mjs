import assert from "node:assert/strict";
import test from "node:test";
import {
  newScheduleFlowId,
  resetScheduleTelemetryForTests,
  scheduleExportTerminal,
  scheduleGenerationTerminal,
  scheduleSaveConflict,
  scheduleSourceLoadTerminal,
  scheduleStepCompleted,
  scheduleStepViewed,
  setScheduleTelemetrySinkForTests,
} from "./schedule-telemetry.ts";

function captureEvents() {
  const events = [];
  const restore = setScheduleTelemetrySinkForTests((event, properties) => events.push({ event, properties }));
  resetScheduleTelemetryForTests();
  return { events, restore };
}

test("schedule telemetry emits only bounded aggregate fields", () => {
  const { events, restore } = captureEvents();
  try {
    const flowId = newScheduleFlowId();
    scheduleStepViewed("courses", flowId, "request-42");
    scheduleStepCompleted("courses", "success", flowId, "request-42");
    scheduleSourceLoadTerminal("curriculum", "success", flowId, "request-42");
    scheduleGenerationTerminal({
      flowId,
      flow: "manual",
      outcome: "incomplete",
      durationMs: 99_999_999,
      requestedCount: 9999,
      returnedCount: -2,
      requestId: "https://evil.example/course/PHYS213",
    });
    scheduleExportTerminal({ flowId, format: "ics", outcome: "success", status: 200, requestId: "request-42" });
    // Runtime validation remains a boundary for an accidental `any` or a
    // future JavaScript call site; enum-like values must not become labels.
    scheduleStepViewed("PHYS213", flowId);
    scheduleSourceLoadTerminal("PHYS213", "success", flowId);
    scheduleGenerationTerminal({
      flowId,
      flow: "PHYS213",
      outcome: "success",
      durationMs: 1,
      requestedCount: 1,
      returnedCount: 1,
    });
    scheduleExportTerminal({ flowId, format: "PHYS213", outcome: "success", status: 200 });

    assert.equal(events.length, 5);
    assert.deepEqual(Object.keys(events[3].properties).sort(), [
      "duration_ms",
      "flow",
      "flow_id",
      "outcome",
      "request_id",
      "requested_count",
      "returned_count",
    ]);
    assert.equal(events[3].properties.duration_ms, 3_600_000);
    assert.equal(events[3].properties.requested_count, 500);
    assert.equal(events[3].properties.returned_count, 0);
    assert.equal(events[3].properties.request_id, null);
    assert.equal(events[4].properties.request_id, "request-42");
    assert.equal(JSON.stringify(events).includes("PHYS213"), false);
  } finally {
    restore();
  }
});

test("terminal schedule events are deduplicated by flow and operation", () => {
  const { events, restore } = captureEvents();
  try {
    const flowId = newScheduleFlowId();
    scheduleStepCompleted("timetable", "success", flowId);
    scheduleStepCompleted("timetable", "error", flowId);
    scheduleSourceLoadTerminal("saved", "success", flowId);
    scheduleSourceLoadTerminal("saved", "error", flowId);
    scheduleGenerationTerminal({
      flowId,
      flow: "automatic",
      outcome: "success",
      durationMs: 1,
      requestedCount: 1,
      returnedCount: 1,
    });
    scheduleGenerationTerminal({
      flowId,
      flow: "automatic",
      outcome: "error",
      durationMs: 2,
      requestedCount: 1,
      returnedCount: 0,
    });
    scheduleSaveConflict(flowId);
    scheduleSaveConflict(flowId);
    scheduleExportTerminal({ flowId, format: "wallpaper", outcome: "success", status: 200 });
    scheduleExportTerminal({ flowId, format: "wallpaper", outcome: "error", status: 500 });

    assert.deepEqual(events.map(({ event }) => event), [
      "schedule_step_completed",
      "schedule_source_load_terminal",
      "schedule_generation_terminal",
      "schedule_save_conflict",
      "schedule_export_terminal",
    ]);
  } finally {
    restore();
  }
});

test("telemetry sink failures never break planner callbacks", () => {
  const restore = setScheduleTelemetrySinkForTests(() => {
    throw new Error("offline analytics");
  });
  try {
    resetScheduleTelemetryForTests();
    const flowId = newScheduleFlowId();
    assert.doesNotThrow(() => {
      scheduleStepViewed("details", flowId);
      scheduleStepCompleted("details", "error", flowId);
      scheduleSourceLoadTerminal("unknown", "unavailable", flowId);
      scheduleGenerationTerminal({
        flowId,
        flow: "what_if",
        outcome: "cancelled",
        durationMs: 4,
        requestedCount: 0,
        returnedCount: 0,
      });
      scheduleSaveConflict(flowId);
      scheduleExportTerminal({ flowId, format: "csv", outcome: "error", status: 503 });
    });
  } finally {
    restore();
  }
});
