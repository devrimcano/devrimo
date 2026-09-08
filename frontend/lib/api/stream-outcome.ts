type StreamEvent = { type: string; runId?: string };
export type StreamOutcome = {
  status: "completed" | "awaiting_confirmation" | "failed" | "interrupted";
  run_id: string | null;
};

/** Report the producer's terminal outcome, independently of HTTP headers. */
export async function* observeChatStream<T extends StreamEvent>(
  events: AsyncIterable<T>,
  report: (outcome: StreamOutcome) => Promise<void>,
): AsyncGenerator<T> {
  let status: StreamOutcome["status"] = "interrupted";
  let failed = false;
  let confirmation = false;
  let runId: string | null = null;
  try {
    for await (const event of events) {
      if (event.type === "run") runId = event.runId ?? null;
      if (event.type === "error") failed = true;
      if (event.type === "confirmation") confirmation = true;
      yield event;
    }
    status = failed ? "failed" : confirmation ? "awaiting_confirmation" : "completed";
  } catch (error) {
    status = "failed";
    throw error;
  } finally {
    // A telemetry outage must not change the stream's result.
    try { await report({ status: failed ? "failed" : status, run_id: runId }); } catch { /* best effort */ }
  }
}
