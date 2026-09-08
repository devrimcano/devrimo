import test from "node:test";
import assert from "node:assert/strict";
import { resolvePlannerIntentStep } from "./planner-intent.ts";

const withCourses = { details: true, courses: true, timetable: false };

test("saved timetable intent stays pending until canonical state is hydrated", () => {
  assert.equal(resolvePlannerIntentStep("timetable", withCourses, false, false), null);
  assert.equal(resolvePlannerIntentStep("timetable", withCourses, true, false), null);
  assert.equal(resolvePlannerIntentStep("timetable", withCourses, false, true), null);
  assert.equal(resolvePlannerIntentStep("timetable", withCourses, true, true), "courses");
});

test("hydrated timetable intent remains timetable when entries make it available", () => {
  assert.equal(resolvePlannerIntentStep("timetable", { ...withCourses, timetable: true }, true, true), "timetable");
});
