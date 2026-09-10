/**
 * A unique id, on every origin the application is reachable from.
 *
 * `crypto.randomUUID` exists only in a secure context. Over plain HTTP -
 * the server's IP, a LAN preview, a proxy that terminates TLS somewhere else -
 * it is `undefined`, and calling it throws.
 *
 * Measured on the deployed build at http://<ip>:3000, signed in:
 *
 *     isSecureContext  false
 *     crypto.randomUUID  undefined
 *
 * and "Programı oluştur" answered with a toast reading "crypto.randomUUID is
 * not a function" - the one button the page exists for, dead, explained in a
 * language no student speaks. lib/telemetry.ts had already been bitten by this
 * and guarded its own call; eleven others had not.
 *
 * Where the value is a UUID for a server that stores it - an idempotency key,
 * a message id - the fallback keeps the shape a UUID has, so nothing
 * downstream has to care which branch produced it.
 */
export function newId(): string {
  const uuid = globalThis.crypto?.randomUUID?.();
  if (uuid) return uuid;
  const bytes = new Uint8Array(16);
  const source = globalThis.crypto?.getRandomValues?.bind(globalThis.crypto);
  if (source) {
    source(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  }
  // Version 4, variant 1: the bits a reader (or a parser) expects to find.
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
