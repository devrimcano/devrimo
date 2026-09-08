import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  calendarBounds,
  dateForEntry,
  dayLabel,
  entryDurationMinutes,
  entryStartMinute,
  formatMinute,
  plannerDayFromDate,
} from "./planner-calendar-logic.ts";

const entry = (overrides = {}) => ({
  id: "entry-1",
  code: "CENG101",
  name: "Computer Engineering",
  section: "1",
  day: "Mon",
  start: 8,
  duration: 1,
  room: "M-101",
  kind: "course",
  ...overrides,
});

test("calendar bounds cover early and evening entries instead of using a fixed window", () => {
  const bounds = calendarBounds([
    entry({ day: "Mon", startMinute: 6 * 60 + 10, durationMinutes: 55 }),
    entry({ id: "entry-2", day: "Fri", startMinute: 20 * 60 + 20, durationMinutes: 95 }),
  ]);

  assert.equal(bounds.minMinute, 6 * 60);
  assert.equal(bounds.maxMinute, 22 * 60);
  assert.equal(bounds.minTime, "06:00:00");
  assert.equal(bounds.maxTime, "22:00:00");
});

test("calendar bounds provide a usable default for an empty timetable", () => {
  assert.deepEqual(calendarBounds([]), {
    minMinute: 8 * 60,
    maxMinute: 18 * 60,
    minTime: "08:00:00",
    maxTime: "18:00:00",
    scrollTime: "08:00:00",
  });
});

test("minute fields take precedence and preserve exact UTC weekday mapping", () => {
  const value = entry({ day: "Wed", start: 10, duration: 2, startMinute: 7 * 60 + 25, durationMinutes: 75 });
  const date = dateForEntry(value);

  assert.equal(entryStartMinute(value), 7 * 60 + 25);
  assert.equal(entryDurationMinutes(value), 75);
  assert.equal(date.getUTCDay(), 3);
  assert.equal(date.getUTCHours(), 7);
  assert.equal(date.getUTCMinutes(), 25);
  assert.equal(plannerDayFromDate(date), "Wed");
});

test("day labels and time formatting remain bilingual and deterministic", () => {
  assert.equal(dayLabel("Thu", "tr"), "Per");
  assert.equal(dayLabel("Thu", "en"), "Thu");
  assert.equal(formatMinute(8 * 60 + 5), "08:05");
  assert.equal(formatMinute(24 * 60 + 5), "00:05");
});

test("calendar selection is routed to the details callback and remains non-destructive", async () => {
  const source = await readFile(new URL("./planner-calendar.tsx", import.meta.url), "utf8");
  const clickHandler = source.match(/const handleClick = \(info: EventClickArg\) => \{[\s\S]*?\n  \};/)?.[0] ?? "";

  assert.match(clickHandler, /selectEntry\(entry\)/);
  assert.doesNotMatch(clickHandler, /onRemoveCourse/);
  assert.match(source, /element\.setAttribute\("role", "button"\)/);
  assert.match(source, /element\.setAttribute\("aria-label", label\)/);
  assert.match(source, /element\.title = label/);
  assert.match(source, /key=\{locale\}/);
  assert.match(source, /event\.key !== "Enter" && event\.key !== " "/);
});
