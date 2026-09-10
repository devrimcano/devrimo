import assert from "node:assert/strict";
import test from "node:test";
import { localPath } from "./safe-next.ts";

const ORIGIN = "https://devrimo.ates.digital";

test("an ordinary path is kept, query and hash included", () => {
  assert.equal(localPath("/schedule", ORIGIN), "/schedule");
  assert.equal(localPath("/schedule?term=20261#week", ORIGIN), "/schedule?term=20261#week");
});

test("a backslash is another origin, whatever it looks like", () => {
  // The spelling that passed the old startsWith test. The URL parser reads it
  // as another site entirely, inheriting only this one's scheme.
  assert.equal(new URL("/\\elsewhere.example", ORIGIN).origin, "https://elsewhere.example");
  assert.equal(localPath("/\\elsewhere.example", ORIGIN), "/");
});

test("a tab inside the path is the same trick", () => {
  assert.equal(localPath("/\t/elsewhere.example", ORIGIN), "/");
});

test("the spellings the old test did catch are still caught", () => {
  assert.equal(localPath("//elsewhere.example", ORIGIN), "/");
  assert.equal(localPath("https://elsewhere.example/pay", ORIGIN), "/");
  assert.equal(localPath("javascript:alert(1)", ORIGIN), "/");
});

test("an absolute url back to this site is allowed, as its path", () => {
  assert.equal(localPath(`${ORIGIN}/settings`, ORIGIN), "/settings");
});

test("nothing, empty and unparseable all mean home", () => {
  assert.equal(localPath(null, ORIGIN), "/");
  assert.equal(localPath("", ORIGIN), "/");
  assert.equal(localPath("http://[", ORIGIN), "/");
});

test("a server render has no origin to trust, so it keeps nobody's absolute url", () => {
  const serverBase = "http://devrimo.invalid";
  assert.equal(localPath("/schedule", serverBase), "/schedule");
  assert.equal(localPath(`${ORIGIN}/schedule`, serverBase), "/");
});
