import assert from "node:assert/strict";
import test from "node:test";
import { newId } from "./new-id.ts";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

test("a secure context returns what the platform gives", () => {
  assert.match(newId(), UUID);
});

test("without randomUUID it is still a version 4 uuid", () => {
  const crypto = globalThis.crypto;
  const stripped = { getRandomValues: crypto.getRandomValues.bind(crypto) };
  Object.defineProperty(globalThis, "crypto", { value: stripped, configurable: true });
  try {
    // The shape the deployed app hits over plain HTTP: no randomUUID at all.
    assert.equal(globalThis.crypto.randomUUID, undefined);
    assert.match(newId(), UUID);
  } finally {
    Object.defineProperty(globalThis, "crypto", { value: crypto, configurable: true });
  }
});

test("without any crypto at all it still answers", () => {
  const crypto = globalThis.crypto;
  Object.defineProperty(globalThis, "crypto", { value: undefined, configurable: true });
  try {
    assert.match(newId(), UUID);
  } finally {
    Object.defineProperty(globalThis, "crypto", { value: crypto, configurable: true });
  }
});

test("two calls are two ids", () => {
  const seen = new Set(Array.from({ length: 500 }, () => newId()));
  assert.equal(seen.size, 500);
});
