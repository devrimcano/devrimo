export type ChatConfirmationRequirement = {
  id: string;
  tool: string;
  arguments: Record<string, unknown>;
};

export type ChatConfirmation = {
  run_id: string;
  session_id: string;
  requirements: ChatConfirmationRequirement[];
};

export type ChatToolEvent = {
  status: "started" | "completed" | "error";
  tool: string | null;
  server: string | null;
  message: string | null;
};

export type ChatStreamError = {
  code: string | null;
  message: string;
};

export type ChatStreamEvent =
  | { type: "run"; runId: string }
  | { type: "text"; delta: string }
  /**
   * A sentence the model produced on its way to a tool call, rather than as
   * part of its answer. The broker holds text back until it is long enough to
   * be an answer and emits it here instead when a tool call follows it, so
   * these belong beside the tool rows in the chain-of-thought and never in
   * front of the reply.
   */
  | { type: "reasoning"; text: string }
  | { type: "confirmation"; confirmation: ChatConfirmation }
  | { type: "tool"; tool: ChatToolEvent }
  | { type: "error"; error: ChatStreamError };

const TOOL_EVENT_STATUS: Record<string, ChatToolEvent["status"]> = {
  tool_call_started: "started",
  tool_call_completed: "completed",
  tool_call_error: "error",
};

/** A persisted SSE frame was not valid for the chat protocol. */
export class ChatStreamParseError extends Error {
  readonly code = "malformed_stream";

  constructor() {
    super("The assistant returned a malformed stream event.");
    this.name = "ChatStreamParseError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function malformed(): never {
  // Deliberately omit the frame: persisted assistant frames can contain
  // conversational or academic content and must never be copied into an error.
  throw new ChatStreamParseError();
}

function parseConfirmation(devrimo: Record<string, unknown>): ChatStreamEvent {
  if (typeof devrimo.run_id !== "string" || typeof devrimo.session_id !== "string") malformed();
  if (!Array.isArray(devrimo.requirements) || devrimo.requirements.length === 0) malformed();

  const requirements = devrimo.requirements.map((requirement) => {
    if (!isRecord(requirement)) malformed();
    if (typeof requirement.id !== "string" || typeof requirement.tool !== "string" || !isRecord(requirement.arguments)) {
      malformed();
    }
    return {
      id: requirement.id,
      tool: requirement.tool,
      arguments: requirement.arguments,
    };
  });

  return {
    type: "confirmation",
    confirmation: {
      run_id: devrimo.run_id,
      session_id: devrimo.session_id,
      requirements,
    },
  };
}

/**
 * Parse one persisted OpenAI-compatible SSE payload.
 *
 * Empty/DONE frames and role-only/usage-only OpenAI frames are protocol noise
 * and remain ignorable. Invalid JSON or malformed required Devrimo events are
 * errors so the stream observer can report a failed turn instead of claiming a
 * clean completion with missing content.
 */
export function parseSseEvent(data: string): ChatStreamEvent | null {
  if (!data || data === "[DONE]") return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    malformed();
  }
  if (!isRecord(parsed)) malformed();

  const rawDevrimo = parsed.devrimo;
  if (rawDevrimo !== undefined && !isRecord(rawDevrimo)) malformed();
  const devrimo = rawDevrimo as Record<string, unknown> | undefined;
  const type = typeof devrimo?.type === "string" ? devrimo.type : undefined;

  if (type === "confirmation_required") return parseConfirmation(devrimo!);

  const toolStatus = type ? TOOL_EVENT_STATUS[type] : undefined;
  if (toolStatus) {
    return {
      type: "tool",
      tool: {
        status: toolStatus,
        tool: typeof devrimo?.tool === "string" ? devrimo.tool : null,
        server: typeof devrimo?.server === "string" ? devrimo.server : null,
        message: typeof devrimo?.message === "string" ? devrimo.message : null,
      },
    };
  }

  if (type === "reasoning") {
    if (typeof devrimo?.text !== "string" || !devrimo.text) malformed();
    return { type: "reasoning", text: devrimo.text };
  }

  if (type === "error") {
    if (typeof devrimo?.message !== "string" || !devrimo.message) malformed();
    return {
      type: "error",
      error: {
        code: typeof devrimo.code === "string" ? devrimo.code : null,
        message: devrimo.message,
      },
    };
  }

  // A chunk with no choices is a valid usage/metadata frame. A present choices
  // field must still have the protocol's array shape.
  if (parsed.choices !== undefined && !Array.isArray(parsed.choices)) malformed();
  const choices = parsed.choices as unknown[] | undefined;
  if (!choices?.length) return null;

  const choice = choices[0];
  if (!isRecord(choice)) malformed();
  const delta = choice.delta;
  const message = choice.message;
  if (delta !== undefined && delta !== null && !isRecord(delta)) malformed();
  if (message !== undefined && message !== null && !isRecord(message)) malformed();
  const deltaContent = isRecord(delta) ? delta.content : undefined;
  const messageContent = isRecord(message) ? message.content : undefined;
  if (deltaContent !== undefined && deltaContent !== null && typeof deltaContent !== "string") malformed();
  if (messageContent !== undefined && messageContent !== null && typeof messageContent !== "string") malformed();
  const content = (typeof deltaContent === "string" ? deltaContent : undefined) ??
    (typeof messageContent === "string" ? messageContent : "");
  return content ? { type: "text", delta: content } : null;
}
