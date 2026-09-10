import assert from "node:assert/strict";
import test from "node:test";

// The module is TypeScript-free at runtime apart from its types, so it is
// loaded the same way the other lib tests load theirs: by reading the source
// and evaluating the one function under test.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "safe-media-url.ts"), "utf8")
  .replace(/export function/g, "function")
  .replace(/: boolean/g, "")
  .replace(/: string/g, "");
const isSafeMediaUrl = new Function(`${source}; return isSafeMediaUrl;`)();

test("a real remote image is allowed", () => {
  assert.equal(isSafeMediaUrl("https://example.com/a.png"), true);
  assert.equal(isSafeMediaUrl("http://example.com/a.png"), true);
});

test("the blob this app makes for itself is allowed", () => {
  assert.equal(isSafeMediaUrl("blob:https://devrimo.example/8f0e"), true);
});

test("an inline image is allowed, and only an image", () => {
  assert.equal(isSafeMediaUrl("data:image/png;base64,iVBORw0KGgo="), true);
  assert.equal(isSafeMediaUrl("data:image/svg+xml,%3Csvg%3E"), true);
  // The one that would be a page rather than a picture.
  assert.equal(isSafeMediaUrl("data:text/html;base64,PHNjcmlwdD4="), false);
});

test("javascript: is refused, however it is dressed up", () => {
  assert.equal(isSafeMediaUrl("javascript:alert(1)"), false);
  assert.equal(isSafeMediaUrl("JavaScript:alert(1)"), false);
  // Tab and newline inside the scheme: what a filter that only compares the
  // first eleven characters misses, and what the URL parser strips.
  assert.equal(isSafeMediaUrl("java\tscript:alert(1)"), false);
  assert.equal(isSafeMediaUrl("java\nscript:alert(1)"), false);
  assert.equal(isSafeMediaUrl("  javascript:alert(1)"), false);
  assert.equal(isSafeMediaUrl("vbscript:msgbox(1)"), false);
});

test("other schemes a message has no business carrying are refused", () => {
  assert.equal(isSafeMediaUrl("file:///etc/passwd"), false);
  assert.equal(isSafeMediaUrl(""), false);
  assert.equal(isSafeMediaUrl("   "), false);
});
