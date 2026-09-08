import test from "node:test";
import assert from "node:assert/strict";
import { plannerCourseAliasMatches, plannerCourseAliases } from "./planner-course-identity.ts";

test("pool removal matches exact display and raw catalog aliases", () => {
  const aliases = plannerCourseAliases({ code: "MATH120", rawCode: "2360120" });
  assert.equal(plannerCourseAliasMatches("MATH120", aliases), true);
  assert.equal(plannerCourseAliasMatches("2360120", aliases), true);
  assert.equal(plannerCourseAliasMatches("MATH1200", aliases), false);
  assert.equal(plannerCourseAliasMatches("1360120", aliases), false);
});
