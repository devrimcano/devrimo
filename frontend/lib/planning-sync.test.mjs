import test from "node:test";
import assert from "node:assert/strict";
import { PlanSaveTracker } from "./planning-sync.ts";

test("save A acknowledgment preserves edit B and B is adopted only for its own response", () => {
  const tracker = new PlanSaveTracker();
  tracker.reset("20261");
  assert.equal(tracker.acknowledge({ term: "20261", revision: 0 }, "empty"), "adopt");
  tracker.submit("A", "20261", "draft A");
  assert.equal(tracker.acknowledge({ term: "20261", revision: 1, idempotency_key: "A" }, "draft B"), "preserve");
  tracker.submit("B", "20261", "draft B");
  assert.equal(tracker.acknowledge({ term: "20261", revision: 2, idempotency_key: "B" }, "draft B"), "adopt");
  assert.equal(tracker.acknowledge({ term: "20261", revision: 1, idempotency_key: "A" }, "draft B"), "ignore");
});

test("batched acknowledgments match keys and ignore late older responses", () => {
  const tracker = new PlanSaveTracker();
  tracker.reset("20261");
  tracker.submit("A", "20261", "A");
  tracker.submit("B", "20261", "B");
  assert.equal(tracker.acknowledge({ term: "20261", revision: 2, idempotency_key: "B" }, "B"), "adopt");
  assert.equal(tracker.acknowledge({ term: "20261", revision: 1, idempotency_key: "A" }, "B"), "ignore");
});

test("retry preserves later edits and term switch rejects old responses", () => {
  const tracker = new PlanSaveTracker();
  tracker.reset("20261");
  tracker.submit("retry-same-key", "20261", "failed draft");
  assert.equal(tracker.acknowledge({ term: "20261", revision: 3, idempotency_key: "retry-same-key" }, "newer draft"), "preserve");
  tracker.reset("20262");
  assert.equal(tracker.acknowledge({ term: "20261", revision: 4 }, "new term"), "ignore");
  assert.equal(tracker.acknowledge({ term: "20262", revision: 0 }, "new term"), "adopt");
});
