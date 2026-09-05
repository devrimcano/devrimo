import { apiFetch } from "@/lib/api/client";
import { getApiBaseUrl } from "@/lib/env";
import { apiErrorFromResponse } from "@/lib/api/fetcher";
import type { ChatCompletionsRequest, ChatMessage, ChatSession } from "@/lib/types";

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
  | { type: "text"; delta: string }
  | { type: "confirmation"; confirmation: ChatConfirmation }
  | { type: "tool"; tool: ChatToolEvent }
  | { type: "error"; error: ChatStreamError };

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

const TOOL_EVENT_STATUS: Record<string, ChatToolEvent["status"]> = {
  tool_call_started: "started",
  tool_call_completed: "completed",
  tool_call_error: "error",
};

function parseSseEvent(data: string): ChatStreamEvent | null {
  if (!data || data === "[DONE]") return null;
  try {
    const json = JSON.parse(data) as {
      choices?: Array<{ delta?: { content?: string }; message?: { content?: string } }>;
      devrimo?: {
        type?: string;
        code?: string;
        message?: string;
        tool?: string;
        server?: string;
        run_id?: string;
        session_id?: string;
        requirements?: ChatConfirmationRequirement[];
      };
    };
    if (
      json.devrimo?.type === "confirmation_required" &&
      json.devrimo.run_id &&
      json.devrimo.session_id
    ) {
      return {
        type: "confirmation",
        confirmation: {
          run_id: json.devrimo.run_id,
          session_id: json.devrimo.session_id,
          requirements: json.devrimo.requirements ?? [],
        },
      };
    }
    // Tool activity and typed errors were previously parsed away here, so a
    // failing campus tool was invisible to the student and to analytics alike.
    const toolStatus = json.devrimo?.type ? TOOL_EVENT_STATUS[json.devrimo.type] : undefined;
    if (toolStatus) {
      return {
        type: "tool",
        tool: {
          status: toolStatus,
          tool: json.devrimo?.tool ?? null,
          server: json.devrimo?.server ?? null,
          message: json.devrimo?.message ?? null,
        },
      };
    }
    if (json.devrimo?.type === "error") {
      return {
        type: "error",
        error: { code: json.devrimo.code ?? null, message: json.devrimo.message ?? "Chat failed" },
      };
    }
    const delta = json.choices?.[0]?.delta?.content ?? json.choices?.[0]?.message?.content ?? "";
    return delta ? { type: "text", delta } : null;
  } catch {
    return null;
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
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...tracingHeaders,
    },
    body: JSON.stringify({
      // The broker picks the model from AGENT_MODEL; this is only a label
      // echoed back on each chunk.
      model: request.model ?? "devrimo",
      messages: request.messages.map(({ role, content }) => ({ role, content })),
      session_id: request.client_id,
      stream: true,
    }),
  });

  if (!response.ok || !response.body) {
    if (response.status === 409) {
      throw new Error("Your agent is answering another message. Please wait.");
    }
    throw await apiErrorFromResponse(response);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data:")) continue;
      const data = trimmed.slice(5).trim();
      if (data === "[DONE]") return;
      const event = parseSseEvent(data);
      if (event) yield event;
    }
  }
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
      Accept: "text/event-stream",
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...tracingHeaders,
    },
    body: JSON.stringify({
      run_id: confirmation.run_id,
      session_id: confirmation.session_id,
      requirement_id: requirementId,
      approved,
    }),
  });
  if (!response.ok || !response.body) {
    throw await apiErrorFromResponse(response);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let text = "";
  let nextConfirmation: ChatConfirmation | null = null;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data:")) continue;
      const event = parseSseEvent(trimmed.slice(5).trim());
      if (event?.type === "text") text += event.delta;
      if (event?.type === "confirmation") nextConfirmation = event.confirmation;
      if (event?.type === "error") throw new Error(event.error.message);
    }
  }
  return { text, confirmation: nextConfirmation };
}
