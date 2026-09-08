import { readPersistedFrames } from "./run-stream";
import { apiFetch } from "@/lib/api/client";
import { getApiBaseUrl } from "@/lib/env";
import { apiErrorFromResponse } from "@/lib/api/fetcher";
import type { ChatCompletionsRequest, ChatMessage, ChatSession } from "@/lib/types";
import { parseSseEvent, type ChatConfirmation, type ChatStreamEvent } from "./chat-stream-parser";
export {
  ChatStreamParseError,
  parseSseEvent,
  type ChatConfirmation,
  type ChatConfirmationRequirement,
  type ChatStreamError,
  type ChatStreamEvent,
  type ChatToolEvent,
} from "./chat-stream-parser";

export type ChatContinuation = {
  text: string;
  confirmation: ChatConfirmation | null;
};

export function listChatSessions(token: string) {
  return apiFetch<{ sessions: ChatSession[] }>("/chat/sessions", { token }).then((data) => data.sessions);
}

export async function getChatSession(token: string, sessionId: string) {
  const data = await apiFetch<ChatSession & { messages: ChatMessage[] }>(`/chat/sessions/${sessionId}`, { token });

  return {
    session: data,
    messages: data.messages,
  };
}

export function deleteChatSession(token: string, sessionId: string) {
  return apiFetch<void>(`/chat/sessions/${sessionId}`, { method: "DELETE", token });
}

export function deleteAllChatSessions(token: string) {
  return apiFetch<{ deleted: number }>("/chat/sessions", { method: "DELETE", token });
}

/** Resume only persisted frames. Reconnecting never repeats a model or email command. */
async function* readRunStream(
  initial: Response,
  token: string,
  tracingHeaders: Record<string, string> = {},
): AsyncGenerator<ChatStreamEvent> {
  if (!initial.ok || !initial.body) throw await apiErrorFromResponse(initial);
  const runId = initial.headers.get("X-Run-ID");
  if (runId) yield { type: "run", runId };
  for await (const data of readPersistedFrames(initial, {
    resume: runId ? (cursor) => fetch(`${getApiBaseUrl()}/api/v1/chat/runs/${runId}/events?after=${cursor}`, {
      headers: {
        Accept: "text/event-stream",
        Authorization: `Bearer ${token}`,
        ...tracingHeaders,
      },
    }) : undefined,
    responseError: apiErrorFromResponse,
  })) {
    const event = parseSseEvent(data);
    if (event) yield event;
  }
}

export async function* streamChatCompletions(
  token: string,
  request: ChatCompletionsRequest,
  tracingHeaders: Record<string, string> = {},
): AsyncGenerator<ChatStreamEvent> {
  const response = await fetch(`${getApiBaseUrl()}/api/v1/chat/completions`, {
    method: "POST",
    headers: {
      Accept: "text/event-stream", "Content-Type": "application/json",
      Authorization: `Bearer ${token}`, ...tracingHeaders,
    },
    body: JSON.stringify({
      model: request.model ?? "devrimo",
      messages: request.messages.map(({ role, content }) => ({ role, content })),
      session_id: request.client_id,
      idempotency_key: request.idempotency_key,
      stream: true,
    }),
  });
  yield* readRunStream(response, token, tracingHeaders);
}

export async function continueChatRun(
  token: string,
  confirmation: ChatConfirmation,
  requirementId: string,
  approved: boolean,
  tracingHeaders: Record<string, string> = {},
): Promise<ChatContinuation> {
  const response = await fetch(`${getApiBaseUrl()}/api/v1/chat/confirmations`, {
    method: "POST",
    headers: {
      Accept: "text/event-stream", "Content-Type": "application/json",
      Authorization: `Bearer ${token}`, ...tracingHeaders,
    },
    body: JSON.stringify({
      run_id: confirmation.run_id, session_id: confirmation.session_id,
      requirement_id: requirementId, approved,
    }),
  });
  let text = "";
  let nextConfirmation: ChatConfirmation | null = null;
  for await (const event of readRunStream(response, token, tracingHeaders)) {
    if (event.type === "text") text += event.delta;
    if (event.type === "confirmation") nextConfirmation = event.confirmation;
    if (event.type === "error") throw new Error(event.error.message);
  }
  return { text, confirmation: nextConfirmation };
}
