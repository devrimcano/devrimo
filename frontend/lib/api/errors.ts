/**
 * A failed API call, carrying enough to find the other side of it.
 *
 * `requestId` is the correlation id the browser minted and the broker echoed
 * back. It is what turns "a student says saving failed" into one query that
 * returns the browser event, this app's proxy log and the broker's issue.
 */
export class ApiError extends Error {
  status: number;
  body: unknown;
  requestId: string | null;

  constructor(message: string, status: number, body?: unknown, requestId?: string | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.requestId = requestId ?? null;
  }
}

export function isNotFound(error: unknown) {
  return error instanceof ApiError && error.status === 404;
}

export function isConflict(error: unknown) {
  return error instanceof ApiError && error.status === 409;
}

/** The correlation id of a failure, when the failure knows one. */
export function requestIdOf(error: unknown): string | null {
  return error instanceof ApiError ? error.requestId : null;
}
