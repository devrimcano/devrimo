import { createUIMessageStream, createUIMessageStreamResponse } from "ai";
import type { UIMessage } from "ai";
import { streamChatCompletions } from "@/lib/api/chat";
import { authenticatedRoute } from "@/lib/api/route-utils";
import { reportServerException, tracingHeadersFrom } from "@/lib/posthog-server";
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
    const textId = "assistant-text";
    // Forwarded to the broker so its LLM traces, exceptions and logs land on the
    // same person, the same session replay and the same correlation id as this
    // browser's events.
    const tracing = tracingHeadersFrom(request, context.requestId);

    const stream = createUIMessageStream({
      execute: async ({ writer }) => {
        writer.write({ type: "text-start", id: textId });
        try {
          for await (const event of streamChatCompletions(
            context.auth.accessToken,
            { messages, client_id: clientId, stream: true },
            tracing,
          )) {
            if (event.type === "text") {
              writer.write({ type: "text-delta", id: textId, delta: event.delta });
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
