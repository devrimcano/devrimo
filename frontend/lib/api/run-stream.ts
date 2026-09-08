type ReplayOptions = {
  resume?: (cursor: number) => Promise<Response>;
  responseError: (response: Response) => Promise<Error>;
  wait?: (milliseconds: number) => Promise<void>;
};

/** A cursor advances only after a complete persisted frame. Never resubmit a command. */
export async function* readPersistedFrames(initial: Response, options: ReplayOptions): AsyncGenerator<string> {
  let cursor = 0;
  let response = initial;
  const wait = options.wait ?? ((ms) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  for (let attempt = 0; attempt < 4; attempt++) {
    if (attempt > 0) {
      await wait(250 * 2 ** (attempt - 1));
      try {
        response = await options.resume!(cursor);
      } catch (error) {
        if (attempt === 3) throw error;
        continue;
      }
    }
    // Authentication and other HTTP failures require caller action, not blind retries.
    if (!response.ok || !response.body) throw await options.responseError(response);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, "\n");
        let boundary: number;
        while ((boundary = buffer.indexOf("\n\n")) >= 0) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const lines = frame.split("\n");
          const id = Number(lines.find((line) => line.startsWith("id:"))?.slice(3).trim());
          const data = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("\n");
          if (!data || (id && id <= cursor)) continue;
          if (id) cursor = id;
          if (data === "[DONE]") return;
          yield data;
        }
      }
    } catch (error) {
      if (!options.resume || attempt === 3) throw error;
    } finally {
      await reader.cancel().catch(() => undefined);
      reader.releaseLock();
    }
    if (!options.resume || attempt === 3) {
      throw new Error("The stream disconnected. Your run is saved; reopen the conversation to check it.");
    }
  }
}
