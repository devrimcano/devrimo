import assert from "node:assert/strict";
import test from "node:test";
import { observeChatStream } from "./stream-outcome.ts";

async function* source(events, failure) {
  yield* events;
  if (failure) throw failure;
}

for (const [name, events, status] of [
  ["complete", [{type: "run", runId: "r1"}, {type: "text", delta: "answer"}], "completed"],
  ["error frame despite clean EOF", [{type: "error"}], "failed"],
  ["pause for confirmation", [{type: "confirmation"}], "awaiting_confirmation"],
]) {
  test(name, async () => {
    const outcomes = [];
    const received = [];
    for await (const event of observeChatStream(source(events), async (result) => { outcomes.push(result); })) received.push(event);
    assert.deepEqual(received, events);
    assert.deepEqual(outcomes, [{status, run_id: name === "complete" ? "r1" : null}]);
  });
}

test("partial stream exception reports failure once and preserves exception", async () => {
  const failure = new Error("transport");
  const outcomes = [];
  await assert.rejects(async () => {
    for await (const event of observeChatStream(source([{type: "text"}], failure), async (result) => { outcomes.push(result); })) void event;
  }, (error) => error === failure);
  assert.deepEqual(outcomes, [{status: "failed", run_id: null}]);
});

test("consumer stops early without claiming completed", async () => {
  const outcomes = [];
  for await (const event of observeChatStream(source([{type: "text"}, {type: "text"}]), async (result) => { outcomes.push(result); })) {
    void event;
    break;
  }
  assert.deepEqual(outcomes, [{status: "interrupted", run_id: null}]);
});

test("telemetry failure cannot break a successful stream", async () => {
  const received = [];
  for await (const event of observeChatStream(source([{type: "text"}]), async () => { throw new Error("telemetry"); })) received.push(event);
  assert.equal(received.length, 1);
});
