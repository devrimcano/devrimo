"use client";

import { useRef, useState, useSyncExternalStore } from "react";
import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { AssistantChatTransport, useChatRuntime } from "@assistant-ui/ai-sdk";
import type { DataUIPart, UIMessage } from "ai";
import { toast } from "sonner";
import { Thread } from "@/components/thread.aui";
import { SessionSidebar } from "@/components/chat/session-sidebar";
import { loadSessionMessages, useChatSessions } from "@/hooks/useChat";
import { Loader2Icon, MenuIcon, Trash2Icon } from "lucide-react";
import { useLocale } from "@/components/locale-provider";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogMedia,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { captureError, captureProductEvent, captureRequestFailure } from "@/components/posthog-analytics";
import type { ChatConfirmation, ChatStreamError, ChatToolEvent } from "@/lib/api/chat";
import { jsonFetch } from "@/lib/api/fetcher";
import { REQUEST_ID_HEADER, newRequestId } from "@/lib/telemetry";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import type { PanelImperativeHandle } from "react-resizable-panels";

const DESKTOP_QUERY = "(min-width: 768px)";

function subscribeToDesktop(callback: () => void) {
  const query = window.matchMedia(DESKTOP_QUERY);
  query.addEventListener("change", callback);
  return () => query.removeEventListener("change", callback);
}

function useDesktopLayout() {
  return useSyncExternalStore(
    subscribeToDesktop,
    () => window.matchMedia(DESKTOP_QUERY).matches,
    () => false,
  );
}

function toUiMessages(sessionId: string, messages: { role: string; content: string; id?: string }[]): UIMessage[] {
  return messages.map((message, index) => ({
    id: message.id ?? `${sessionId}-${index}`,
    role: message.role as UIMessage["role"],
    parts: [{ type: "text", text: message.content }],
  }));
}

class ChatTelemetry {
  private requestStartedAt: number | null = null;
  private streamError: ChatStreamError | null = null;
  private requestId: string | null = null;

  readonly transport: AssistantChatTransport<UIMessage>;

  constructor() {
    this.transport = new AssistantChatTransport({
      api: "/api/chat",
      prepareSendMessagesRequest: ({ id, messages }) => {
        const latestMessage = messages.at(-1);
        const textLength = latestMessage?.parts.reduce(
          (total, part) => total + (part.type === "text" ? part.text.length : 0),
          0,
        ) ?? 0;
        const attachmentCount = latestMessage?.parts.filter((part) => part.type === "file").length ?? 0;
        this.requestStartedAt = Date.now();
        this.streamError = null;
        // The AI SDK transport does not go through `jsonFetch`, so the
        // correlation id every other request in this app carries has to be
        // minted here. PostHog's own tracing headers ride along automatically.
        this.requestId = newRequestId();
        captureProductEvent("chat_message_sent", {
          conversation_type: id ? "existing" : "new",
          message_position: messages.length,
          text_length: textLength,
          attachment_count: attachmentCount,
          request_id: this.requestId,
        });
        return { body: { id, messages }, headers: { [REQUEST_ID_HEADER]: this.requestId } };
      },
    });
  }

  /** The correlation id of the turn in flight, for the events that report it. */
  currentRequestId() {
    return this.requestId;
  }

  finishDuration() {
    const startedAt = this.requestStartedAt;
    this.requestStartedAt = null;
    return startedAt ? Math.max(0, Math.round((Date.now() - startedAt) / 1000)) : null;
  }

  setStreamError(error: ChatStreamError) {
    this.streamError = error;
  }

  /** The broker's typed error for this turn, consumed once by `onError`. */
  takeStreamError() {
    const error = this.streamError;
    this.streamError = null;
    return error;
  }
}

function AssistantThread({
  threadId,
  initialMessages,
  onThreadReady,
}: {
  threadId?: string;
  initialMessages?: UIMessage[];
  onThreadReady: (id: string | undefined) => void;
}) {
  const [telemetry] = useState(() => new ChatTelemetry());
  const [pendingConfirmation, setPendingConfirmation] = useState<ChatConfirmation | null>(null);
  const [confirmationPending, setConfirmationPending] = useState(false);
  const { pick, locale } = useLocale();

  const runtime = useChatRuntime({
    id: threadId,
    messages: initialMessages,
    transport: telemetry.transport,
    onThreadIdChange: onThreadReady,
    onFinish: ({ isError }) => {
      // AI SDK invokes onFinish for both success and failure. Error analytics
      // are emitted by onError, so counting this as completed would corrupt
      // the completion-rate denominator.
      if (isError) return;
      captureProductEvent("chat_response_completed", {
        duration_seconds: telemetry.finishDuration() ?? 0,
        request_id: telemetry.currentRequestId(),
      });
    },
    onData: (part: DataUIPart<Record<string, unknown>>) => {
      if (part.type === "data-tool") {
        // Tool activity the broker streams. The server records the
        // authoritative $ai_span; this is the student-visible half — what the
        // UI was told, and when.
        const tool = part.data as unknown as ChatToolEvent;
        captureProductEvent("agent_tool_call", {
          tool: tool.tool,
          server: tool.server,
          status: tool.status,
        });
        return;
      }
      if (part.type === "data-stream-error") {
        telemetry.setStreamError(part.data as unknown as ChatStreamError);
        return;
      }
      if (part.type !== "data-confirmation") return;
      const confirmation = part.data as ChatConfirmation;
      captureProductEvent("chat_confirmation_shown", {
        tool: confirmation.requirements[0]?.tool ?? null,
      });
      setPendingConfirmation(confirmation);
    },
    onError: (error) => {
      const message = error instanceof Error ? error.message : "Chat failed";
      // The broker now sends a typed code on its error chunks, so this no
      // longer has to guess an error's nature by searching its prose.
      const streamError = telemetry.takeStreamError();
      const busy =
        streamError?.code === "agent_busy" ||
        message.includes("409") ||
        message.toLowerCase().includes("busy");
      const network = !busy && /failed to fetch|network|load failed/i.test(message);
      captureProductEvent("chat_response_error", {
        category: busy ? "busy" : network ? "network" : "other",
        status: busy ? 409 : null,
        error_code: streamError?.code ?? null,
        duration_seconds: telemetry.finishDuration(),
        request_id: telemetry.currentRequestId(),
      });
      captureError(error, {
        source: "chat_stream",
        error_code: streamError?.code ?? null,
        request_id: telemetry.currentRequestId(),
      });
      toast.error(
        busy
          ? pick({ tr: "Asistan şu anda önceki yanıtını hazırlıyor. Lütfen bekle.", en: "Your assistant is still responding to the previous message. Please wait." })
          : message,
      );
    },
  });

  const requirement = pendingConfirmation?.requirements[0];

  async function resolveConfirmation(approved: boolean) {
    if (!pendingConfirmation || !requirement || confirmationPending) return;
    setConfirmationPending(true);
    try {
      const payload = await jsonFetch<{
        text?: string;
        confirmation?: ChatConfirmation | null;
      }>("/api/chat/confirm", {
        method: "POST",
        body: {
          confirmation: pendingConfirmation,
          requirement_id: requirement.id,
          approved,
        },
      });
      const continuationText =
        payload.text ||
        (!payload.confirmation
          ? approved
            ? pick({ tr: "İşlem tamamlandı.", en: "The action was completed." })
            : pick({ tr: "İşlem iptal edildi.", en: "The action was cancelled." })
          : "");
      if (continuationText) {
        runtime.thread.append({
          role: "assistant",
          content: [{ type: "text", text: continuationText }],
        });
      }
      setPendingConfirmation(payload.confirmation ?? null);
      captureProductEvent("agent_action_confirmation", {
        approved,
        tool: requirement.tool,
        result: "completed",
        awaiting_confirmation: Boolean(payload.confirmation),
      });
    } catch (error) {
      captureProductEvent("agent_action_confirmation", {
        approved,
        tool: requirement.tool,
        result: "failed",
        awaiting_confirmation: false,
      });
      captureRequestFailure(error, { operation: "chat.confirm", kind: "mutation" });
      captureError(error, { source: "chat_confirmation" });
      toast.error(error instanceof Error ? error.message : "Confirmation failed");
    } finally {
      setConfirmationPending(false);
    }
  }

  const actionTitle = requirement?.tool?.includes("send_mail")
    ? pick({ tr: "E-posta Gönderme Onayı", en: "Send Email Confirmation" })
    : pick({ tr: "İşlem Onayı", en: "Action Confirmation" });

  return (
    <>
      <AssistantRuntimeProvider runtime={runtime}>
        <Thread />
      </AssistantRuntimeProvider>
      <AlertDialog open={Boolean(pendingConfirmation)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{actionTitle}</AlertDialogTitle>
            <AlertDialogDescription>
              {pick({
                tr: "Yapılacak işlemi ve bilgileri kontrol edip onayla.",
                en: "Please review the action details before confirming.",
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="max-h-72 space-y-2 overflow-auto rounded-lg border bg-muted/40 p-3 text-xs">
            {requirement?.arguments && typeof requirement.arguments === "object" ? (
              Object.entries(requirement.arguments as Record<string, unknown>).map(([k, v]) => (
                <div key={k} className="flex flex-col gap-0.5">
                  <span className="font-semibold text-foreground/80 capitalize">{k.replace(/_/g, " ")}:</span>
                  <span className="whitespace-pre-wrap text-muted-foreground">{typeof v === "object" ? JSON.stringify(v, null, 2) : String(v)}</span>
                </div>
              ))
            ) : (
              <pre className="whitespace-pre-wrap">{JSON.stringify(requirement?.arguments ?? {}, null, 2)}</pre>
            )}
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={confirmationPending} onClick={() => void resolveConfirmation(false)}>
              {pick({ tr: "Vazgeç", en: "Cancel" })}
            </AlertDialogCancel>
            <AlertDialogAction disabled={confirmationPending} onClick={() => void resolveConfirmation(true)}>
              {confirmationPending ? <Loader2Icon className="animate-spin" /> : null}
              {pick({ tr: "Onayla", en: "Approve" })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

export function ChatShell() {
  const { pick } = useLocale();
  const desktop = useDesktopLayout();
  const { sessions, remove, refetch } = useChatSessions();
  const [threadId, setThreadId] = useState<string | undefined>(undefined);
  const [selectedSessionId, setSelectedSessionId] = useState<string | undefined>(undefined);
  const [seedMessages, setSeedMessages] = useState<UIMessage[] | undefined>(undefined);
  const [chatKey, setChatKey] = useState(0);
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  const [mobileHistoryOpen, setMobileHistoryOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const sidebarPanelRef = useRef<PanelImperativeHandle | null>(null);
  const pendingDeleteSession = sessions.find((session) => session.id === pendingDeleteId);

  async function selectSession(sessionId: string) {
    try {
      const history = await loadSessionMessages(sessionId);
      setSeedMessages(toUiMessages(sessionId, history));
      setThreadId(sessionId);
      setSelectedSessionId(sessionId);
      setChatKey((value) => value + 1);
      captureProductEvent("chat_opened", { source: "history" });
    } catch (error) {
      captureError(error, { source: "chat_load_session" });
      toast.error(error instanceof Error ? error.message : "Could not load session");
    }
  }

  function newChat() {
    setSeedMessages([]);
    setThreadId(undefined);
    setSelectedSessionId(undefined);
    setChatKey((value) => value + 1);
  }

  function startNewChat() {
    captureProductEvent("chat_new_clicked", {});
    newChat();
  }

  async function deleteSession(sessionId: string) {
    try {
      const wasActive = selectedSessionId === sessionId;
      await remove.mutateAsync(sessionId);
      if (wasActive) newChat();
      setPendingDeleteId(null);
      captureProductEvent("chat_deleted", { was_active: wasActive });
      toast.success(pick({ tr: "Sohbet silindi.", en: "Chat deleted." }));
    } catch (error) {
      captureError(error, { source: "chat_delete_session" });
      toast.error(error instanceof Error ? error.message : "Could not delete session");
    }
  }

  const assistantThread = (
    <AssistantThread
      key={chatKey}
      threadId={threadId}
      initialMessages={seedMessages}
      onThreadReady={(nextId) => {
        setThreadId(nextId);
        if (!selectedSessionId && nextId) setSelectedSessionId(nextId);
        void refetch();
      }}
    />
  );

  const requestDelete = (sessionId: string) => {
    captureProductEvent("chat_delete_requested", {
      was_active: selectedSessionId === sessionId,
    });
    setPendingDeleteId(sessionId);
  };

  return (
    <div className="flex h-full min-h-0 bg-[radial-gradient(circle_at_70%_0%,rgb(215_25_63/3.5%),transparent_32%)] dark:bg-[radial-gradient(circle_at_70%_0%,rgb(238_49_84/7%),transparent_34%)]">
      {desktop ? (
        <ResizablePanelGroup orientation="horizontal" className="min-h-0">
          <ResizablePanel
            id="chat-sidebar"
            panelRef={sidebarPanelRef}
            defaultSize="280px"
            minSize="220px"
            maxSize="380px"
            collapsedSize="56px"
            collapsible
            groupResizeBehavior="preserve-pixel-size"
            onResize={(size) => setSidebarCollapsed(size.inPixels < 100)}
          >
            <SessionSidebar
              className="w-full"
              collapsed={sidebarCollapsed}
              onToggleCollapse={() => {
                if (sidebarCollapsed) sidebarPanelRef.current?.expand();
                else sidebarPanelRef.current?.collapse();
              }}
              sessions={sessions}
              activeId={selectedSessionId}
              onNewChat={startNewChat}
              onSelect={selectSession}
              onDelete={requestDelete}
            />
          </ResizablePanel>
          <ResizableHandle
            withHandle={!sidebarCollapsed}
            aria-label={pick({ tr: "Sohbet kenar çubuğunu yeniden boyutlandır", en: "Resize chat sidebar" })}
            className="bg-sidebar-border hover:bg-primary/30 focus-visible:bg-primary/30"
          />
          <ResizablePanel id="chat-content" minSize="320px">
            <div className="relative h-full min-w-0">{assistantThread}</div>
          </ResizablePanel>
        </ResizablePanelGroup>
      ) : (
        <div className="relative min-w-0 flex-1">
          <Button
            variant="outline"
            size="icon"
            className="absolute left-3 top-3 z-20 bg-card/90 shadow-sm backdrop-blur"
            onClick={() => setMobileHistoryOpen(true)}
            aria-label={pick({ tr: "Sohbet geçmişini aç", en: "Open chat history" })}
          >
            <MenuIcon />
          </Button>
          {assistantThread}
        </div>
      )}
      <Sheet open={mobileHistoryOpen} onOpenChange={setMobileHistoryOpen}>
        <SheetContent side="left" className="w-[19rem] max-w-[88vw] gap-0 bg-sidebar p-0">
          <SheetHeader className="border-b">
            <SheetTitle>{pick({ tr: "Sohbetler", en: "Chats" })}</SheetTitle>
            <SheetDescription>{pick({ tr: "Geçmiş bir sohbeti aç veya yeni bir sohbet başlat.", en: "Open a previous chat or start a new one." })}</SheetDescription>
          </SheetHeader>
          <SessionSidebar
            className="min-h-0 w-full flex-1 border-r-0"
            sessions={sessions}
            activeId={selectedSessionId}
            onNewChat={() => { startNewChat(); setMobileHistoryOpen(false); }}
            onSelect={(sessionId) => { void selectSession(sessionId); setMobileHistoryOpen(false); }}
            onDelete={(sessionId) => {
              requestDelete(sessionId);
              setMobileHistoryOpen(false);
            }}
          />
        </SheetContent>
      </Sheet>
      <AlertDialog
        open={Boolean(pendingDeleteId)}
        onOpenChange={(open) => {
          if (!open && !remove.isPending) setPendingDeleteId(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogMedia className="bg-destructive/10 text-destructive">
              <Trash2Icon />
            </AlertDialogMedia>
            <AlertDialogTitle>
              {pick({ tr: "Bu sohbet silinsin mi?", en: "Delete this chat?" })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {pendingDeleteSession?.title?.trim()
                ? pick({
                    tr: `“${pendingDeleteSession.title}” kalıcı olarak silinecek. Bu işlem geri alınamaz.`,
                    en: `“${pendingDeleteSession.title}” will be permanently deleted. This action cannot be undone.`,
                  })
                : pick({
                    tr: "Bu sohbet kalıcı olarak silinecek. Bu işlem geri alınamaz.",
                    en: "This chat will be permanently deleted. This action cannot be undone.",
                  })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={remove.isPending}>
              {pick({ tr: "Vazgeç", en: "Cancel" })}
            </AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={remove.isPending || !pendingDeleteId}
              onClick={() => {
                if (pendingDeleteId) void deleteSession(pendingDeleteId);
              }}
            >
              {remove.isPending ? <Loader2Icon className="animate-spin" /> : <Trash2Icon />}
              {pick({ tr: "Sohbeti sil", en: "Delete chat" })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
