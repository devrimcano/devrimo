import { createHash } from "node:crypto";
import { createUIMessageStream, createUIMessageStreamResponse } from "ai";
import type { UIMessage } from "ai";
import { streamChatCompletions } from "@/lib/api/chat";
import { observeChatStream } from "@/lib/api/stream-outcome";
import { authenticatedRoute } from "@/lib/api/route-utils";
import { reportServerEvent, reportServerException, tracingHeadersFrom } from "@/lib/posthog-server";
import { OUTCOME_SUCCESS, OUTCOME_UNEXPECTED_FAILURE } from "@/lib/telemetry";
import type { ChatMessage, ChatRole } from "@/lib/types";

function textFromParts(message: UIMessage) {
  return message.parts
    .filter((part): part is { type: "text"; text: string } => part.type === "text")
    .map((part) => part.text)
    .join("");
}

function toChatMessages(messages: UIMessage[]): ChatMessage[] {
  return messages
    .filter((message) => message.role === "user" || message.role === "assistant" || message.role === "system")
    .map((message) => ({
      role: message.role as ChatRole,
      content: textFromParts(message),
    }))
    .filter((message) => message.content.trim().length > 0);
}

export const maxDuration = 600;

export const POST = authenticatedRoute(
  "/api/chat",
  async (context, request) => {
    const body = (await request.json()) as {
      messages?: UIMessage[];
      id?: string;
    };

    const messages = toChatMessages(body.messages ?? []);
    const clientId = body.id;
    const messageId = body.messages?.findLast((message) => message.role === "user")?.id;
    const idempotencyKey = messageId ? createHash("sha256").update(`${clientId}:${messageId}`).digest("hex") : undefined;
    const textId = "assistant-text";
    // Forwarded to the broker so its LLM traces, exceptions and logs land on the
    // same person, the same session replay and the same correlation id as this
    // browser's events.
    const tracing = tracingHeadersFrom(request, context.requestId);

    const stream = createUIMessageStream({
      execute: async ({ writer }) => {
        const started = Date.now();
        let reasoningParts = 0;
        writer.write({ type: "text-start", id: textId });
        try {
          for await (const event of observeChatStream(streamChatCompletions(
            context.auth.accessToken,
            { messages, client_id: clientId, idempotency_key: idempotencyKey, stream: true },
            tracing,
          ), (result) => reportServerEvent("chat_stream_completed", {
            requestId: context.requestId,
            distinctId: context.distinctId,
            sessionId: context.sessionId,
            route: context.route,
            ...result,
            outcome: result.status === "completed" || result.status === "awaiting_confirmation"
              ? OUTCOME_SUCCESS : OUTCOME_UNEXPECTED_FAILURE,
            duration_seconds: (Date.now() - started) / 1000,
          }))) {
            if (event.type === "run") {
              writer.write({ type: "data-run", data: { runId: event.runId } });
            } else if (event.type === "text") {
              writer.write({ type: "text-delta", id: textId, delta: event.delta });
            } else if (event.type === "reasoning") {
              // What the model said on its way to a tool call. Its own part, so
              // the thread groups it into the chain-of-thought beside that
              // tool's row instead of stacking it in front of the answer -
              // which is where four copies of "PHYS 213 şubelerini kontrol
              // ediyorum." used to end up, run together without spaces.
              const id = `reasoning-${reasoningParts++}`;
              writer.write({ type: "reasoning-start", id });
              writer.write({ type: "reasoning-delta", id, delta: event.text });
              writer.write({ type: "reasoning-end", id });
            } else if (event.type === "confirmation") {
              writer.write({ type: "data-confirmation", data: event.confirmation });
            } else if (event.type === "tool") {
              writer.write({ type: "data-tool", data: event.tool });
            } else {
              // Preserve the typed code for product analytics, then emit the AI
              // SDK's standard error chunk so the runtime actually enters its
              // error path and the student sees the failure.
              writer.write({ type: "data-stream-error", data: event.error });
              writer.write({ type: "error", errorText: event.error.message });
            }
          }
        } catch (error) {
          const message = error instanceof Error ? error.message : "Chat failed";
          // Previously this message went into the stream and nowhere else: a
          // broker that was down produced a toast and no record anywhere.
          // Awaited, because the stream body outlives the handler and an
          // unflushed report from inside it is a report that never happens.
          await reportServerException(error, {
            requestId: context.requestId,
            distinctId: context.distinctId,
            sessionId: context.sessionId,
            source: "chat_proxy",
            // The chat session the student sees, which is deliberately not the
            // browser replay session in `sessionId`.
            chat_session_id: clientId ?? null,
          });
          writer.write({ type: "error", errorText: message });
        }
        writer.write({ type: "text-end", id: textId });
      },
    });

    return createUIMessageStreamResponse({ stream });
  },
  { streaming: true },
);
