import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// The registry is plain data with a few pure helpers, so it is loaded the way
// the other lib tests load theirs: source in, types stripped, evaluate.
const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "documents.ts"), "utf8");
const runnable = source
  .replace(/^export type [\s\S]*?^};$/gm, "")
  .replace(/^export /gm, "")
  .replace(/: readonly LegalDocument\[\]/g, "")
  .replace(/\): \{ id: string; version: string \}/g, ")")
  .replace(/\(document: LegalDocument\)/g, "(document)")
  .replace(/\(slug: string\)/g, "(slug)")
  .replace(/: LegalDocument \| undefined/g, "")
  .replace(/: number/g, "")
  .replace(/\(total, section\)/g, "(total, section)");
const registry = new Function(
  `${runnable}; return { LEGAL_DOCUMENTS, PLACEHOLDER, findDocument, placeholderCount, consentTarget };`,
)();
const { LEGAL_DOCUMENTS, findDocument, placeholderCount, consentTarget } = registry;

test("every document is reachable by its own slug", () => {
  for (const document of LEGAL_DOCUMENTS) {
    assert.equal(findDocument(document.slug)?.id, document.id);
  }
  assert.equal(findDocument("yok-boyle-bir-sey"), undefined);
});

test("slugs and ids are unique, or a consent record points at two things", () => {
  const slugs = LEGAL_DOCUMENTS.map((d) => d.slug);
  const ids = LEGAL_DOCUMENTS.map((d) => d.id);
  assert.equal(new Set(slugs).size, slugs.length);
  assert.equal(new Set(ids).size, ids.length);
});

test("a version is date-ordered and specific enough to distinguish same-day edits", () => {
  for (const document of LEGAL_DOCUMENTS) {
    assert.match(document.version, /^\d{4}-\d{2}-\d{2}\.\d+$/, `${document.id} has an unusable version`);
    assert.match(document.effectiveFrom, /^\d{4}-\d{2}-\d{2}$/);
    assert.ok(document.version.startsWith(document.effectiveFrom));
  }
});

test("a draft cannot be the target of a recorded consent", () => {
  const draft = LEGAL_DOCUMENTS.find((d) => d.status === "draft");
  assert.ok(draft, "this test needs at least one draft to be meaningful");
  assert.throws(() => consentTarget(draft), /draft/);
});

test("a published document yields exactly what a consent record stores", () => {
  const published = { ...LEGAL_DOCUMENTS[0], status: "published" };
  assert.deepEqual(consentTarget(published), { id: published.id, version: published.version });
});

test("the drafts are honest about how much is unwritten", () => {
  for (const document of LEGAL_DOCUMENTS.filter((d) => d.status === "draft")) {
    assert.ok(
      placeholderCount(document) > 0,
      `${document.id} is marked draft but has nothing left to write — publish it or fill it`,
    );
  }
});

test("a published document has no unwritten spans left in it", () => {
  for (const document of LEGAL_DOCUMENTS.filter((d) => d.status === "published")) {
    assert.equal(
      placeholderCount(document),
      0,
      `${document.id} is published while still carrying a [[placeholder]]`,
    );
  }
});
