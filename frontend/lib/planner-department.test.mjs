import assert from "node:assert/strict";
import test from "node:test";
import ts from "typescript";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const source = readFileSync(new URL("./planner-department.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const module = { exports: {} };
vm.runInNewContext(compiled, { module, exports: module.exports });
const { resolvePlannerDepartment } = module.exports;

test("student context wins when it arrives before saved state", () => {
  const result = resolvePlannerDepartment({ code: "567", label: "EE" }, { code: "", label: "" });
  assert.deepEqual({ ...result }, { code: "567", label: "EE", source: "student-context" });
});

test("student context wins when it arrives after saved state", () => {
  const before = resolvePlannerDepartment(null, { code: "571", label: "CENG" });
  assert.deepEqual({ ...before }, { code: "571", label: "CENG", source: "fallback" });
  const after = resolvePlannerDepartment({ code: "567", label: "EE" }, { code: "571", label: "CENG" });
  assert.deepEqual({ ...after }, { code: "567", label: "EE", source: "student-context" });
});
