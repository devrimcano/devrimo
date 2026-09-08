import assert from "node:assert/strict";
import test from "node:test";
import { ChatStreamParseError, parseSseEvent } from "./chat-stream-parser.ts";

test("malformed JSON fails the stream parser without exposing the frame", () => {
  assert.throws(() => parseSseEvent('{"choices":'), (error) => {
    assert.ok(error instanceof ChatStreamParseError);
    assert.equal(error.code, "malformed_stream");
    assert.equal(error.message, "The assistant returned a malformed stream event.");
    return true;
  });
});

test("malformed required confirmation events fail instead of becoming empty content", () => {
  assert.throws(
    () => parseSseEvent(JSON.stringify({ devrimo: { type: "confirmation_required", run_id: "run-1" } })),
    ChatStreamParseError,
  );
});

test("role-only and usage frames remain ignorable", () => {
  assert.equal(parseSseEvent(JSON.stringify({ choices: [{ delta: { role: "assistant" } }] })), null);
  assert.equal(parseSseEvent(JSON.stringify({ choices: [] })), null);
  assert.equal(parseSseEvent("[DONE]"), null);
});

test("valid text and tool frames retain their typed protocol events", () => {
  assert.deepEqual(
    parseSseEvent(JSON.stringify({ choices: [{ delta: { content: "hello" } }] })),
    { type: "text", delta: "hello" },
  );
  assert.deepEqual(
    parseSseEvent(JSON.stringify({ devrimo: { type: "tool_call_error", tool: "sais" } })),
    { type: "tool", tool: { status: "error", tool: "sais", server: null, message: null } },
  );
});
