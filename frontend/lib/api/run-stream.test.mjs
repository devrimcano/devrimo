import assert from "node:assert/strict";
import test from "node:test";
import { readPersistedFrames } from "./run-stream.ts";

function response(chunks, interrupted = false) {
  const encoder = new TextEncoder();
  let index = 0;
  return new Response(new ReadableStream({
    pull(controller) {
      if (index < chunks.length) controller.enqueue(encoder.encode(chunks[index++]));
      else if (interrupted) controller.error(new Error("connection interrupted"));
      else controller.close();
    },
  }));
}

const responseError = async (result) => new Error(`HTTP ${result.status}`);

test("replay retains complete cursor through partial frame and failed GET, suppressing duplicates", async () => {
  const cursors = [];
  const waits = [];
  const actual = [];
  for await (const frame of readPersistedFrames(response([
    'id: 1\r\ndata: {"text":"first"}\r',
    '\n\r\nid: 2\ndata: {"text":"incomplete',
  ], true), {
    responseError,
    wait: async (ms) => { waits.push(ms); },
    resume: async (cursor) => {
      cursors.push(cursor);
      if (cursors.length === 1) throw new TypeError("network unavailable");
      return response([
        'id: 1\ndata: {"text":"first"}\n\nid: 2\ndata: {"text":"second"}\n\n',
        'id: 3\ndata: [DONE]\n\n',
      ]);
    },
  })) actual.push(frame);
  assert.deepEqual(actual, ['{"text":"first"}', '{"text":"second"}']);
  assert.deepEqual(cursors, [1, 1]);
  assert.deepEqual(waits, [250, 500]);
});

test("network replay failures stop after three bounded attempts", async () => {
  let requests = 0;
  await assert.rejects(async () => {
    for await (const frame of readPersistedFrames(response([]), {
      responseError, wait: async () => {},
      resume: async () => { requests++; throw new Error("offline"); },
    })) void frame;
  }, /offline/);
  assert.equal(requests, 3);
});

test("HTTP authorization failure is surfaced without retrying", async () => {
  let requests = 0;
  await assert.rejects(async () => {
    for await (const frame of readPersistedFrames(response([]), {
      responseError, wait: async () => {},
      resume: async () => { requests++; return new Response(null, { status: 401 }); },
    })) void frame;
  }, /HTTP 401/);
  assert.equal(requests, 1);
});
