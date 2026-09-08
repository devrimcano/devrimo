import assert from "node:assert/strict";
import test from "node:test";
import ts from "typescript";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("./use-planning.ts", import.meta.url), "utf8");
const functionSource = source.match(
  /export function isRetryablePlanningFailure[\s\S]*?\n}/,
)?.[0];
assert.ok(functionSource, "retry classification function is present");
const compiled = ts.transpileModule(functionSource, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
// Named anything but `module`: Next's lint rule reserves that identifier, and
// the compiled CommonJS output only cares what it is passed, not what the
// binding here is called.
const sandbox = { exports: {} };
new Function("module", "exports", compiled)(sandbox, sandbox.exports);
const { isRetryablePlanningFailure } = sandbox.exports;

test("validation failure does not lock later schedule edits", () => {
  assert.equal(isRetryablePlanningFailure(422), false);
});

test("transient and network failures keep the exact command for retry", () => {
  assert.equal(isRetryablePlanningFailure(null), true);
  assert.equal(isRetryablePlanningFailure(408), true);
  assert.equal(isRetryablePlanningFailure(429), true);
  assert.equal(isRetryablePlanningFailure(503), true);
});
