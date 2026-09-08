"use client";

import { useEffect, useRef, useState, useSyncExternalStore, type MutableRefObject } from "react";
import { AssistantRuntimeProvider, useAuiState } from "@assistant-ui/react";
import { AssistantChatTransport, useChatRuntime } from "@assistant-ui/ai-sdk";
import type { DataUIPart, UIMessage } from "ai";
import { toast } from "sonner";
import { Thread } from "@/components/thread.aui";
import { SessionSidebar } from "@/components/chat/session-sidebar";
import { loadSessionMessages, useChatSessions } from "@/hooks/useChat";
import { Loader2Icon, MenuIcon, OctagonAlertIcon, RotateCcwIcon, Trash2Icon, XIcon } from "lucide-react";
import { useLocale } from "@/components/locale-provider";
import { activityFields } from "@/lib/agent-activity";
import { CampusStatusNotice } from "@/components/chat/campus-status-notice";
import { cn } from "@/lib/utils";
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
import { requestIdOf } from "@/lib/api/errors";
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
  private activeRun: string | null = null;
  private pendingStop = false;
  private cancellingRun: string | null = null;
  private requestStartedAt: number | null = null;
  private streamError: ChatStreamError | null = null;
  private requestId: string | null = null;

  readonly transport: AssistantChatTransport<UIMessage>;

  constructor() {
    this.transport = new AssistantChatTransport({
      api: "/api/chat",
      prepareSendMessagesRequest: ({ id, messages }) => {
        this.activeRun = null;
        this.pendingStop = false;
        this.cancellingRun = null;
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

  attachRun(runId: string) {
    this.activeRun = runId;
    return this.pendingStop;
  }

  requestStop() {
    this.pendingStop = true;
    if (!this.activeRun || this.cancellingRun === this.activeRun) return null;
    this.cancellingRun = this.activeRun;
    return this.activeRun;
  }

  isCurrentRun(runId: string) {
    return this.activeRun === runId;
  }

  finishStop(runId: string) {
    if (this.cancellingRun === runId) this.cancellingRun = null;
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

/**
 * Reports whether a run is in flight to a parent outside the runtime provider.
 *
 * Selecting a session while an answer was streaming used to remount the thread
 * and drop the answer with no warning, because the only component that knew a
 * run was open was inside the provider and the sidebar is not.
 */
function RunStateWatcher({ onChange }: { onChange?: (running: boolean) => void }) {
  const running = useAuiState((s) => s.thread.isRunning);
  useEffect(() => {
    onChange?.(running);
  }, [running, onChange]);
  return null;
}

type StreamFailure = { text: string; kind: "busy" | "network" | "other" };

function AssistantThread({
  threadId,
  initialMessages,
  onThreadReady,
  onRunningChange,
  cancelRef,
}: {
  threadId?: string;
  initialMessages?: UIMessage[];
  onThreadReady: (id: string | undefined) => void;
  onRunningChange?: (running: boolean) => void;
  cancelRef?: MutableRefObject<(() => Promise<void>) | null>;
}) {
  const [telemetry] = useState(() => new ChatTelemetry());
  // A failed run is kept until the student does something about it. It used to
  // be a four-second toast carrying the raw error, positioned over the composer
  // on mobile, so the one state with the least design in the surface was also
  // the one a campus failure lands in.
  const [streamFailure, setStreamFailure] = useState<StreamFailure | null>(null);
  const [pendingConfirmation, setPendingConfirmation] = useState<ChatConfirmation | null>(null);
  const [confirmationPending, setConfirmationPending] = useState(false);
  const { pick } = useLocale();

  const runtime = useChatRuntime({
    id: threadId,
    messages: initialMessages,
    transport: telemetry.transport,
    onThreadIdChange: onThreadReady,
    onFinish: ({ isError, isAbort }) => {
      // AI SDK invokes onFinish for both success and failure. Error analytics
      // are emitted by onError, so counting this as completed would corrupt
      // the completion-rate denominator.
      if (isError || isAbort) return;
      captureProductEvent("chat_response_completed", {
        duration_seconds: telemetry.finishDuration() ?? 0,
        request_id: telemetry.currentRequestId(),
      });
    },
    onData: (part: DataUIPart<Record<string, unknown>>) => {
      if (part.type === "data-run") {
        // A new run is the student's answer to the last failure.
        setStreamFailure(null);
        if (telemetry.attachRun((part.data as { runId: string }).runId)) void cancelCurrentRun();
        return;
      }
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
      setStreamFailure({
        kind: busy ? "busy" : network ? "network" : "other",
        // The broker's own sentence is kept whole. It is what the student can
        // quote when they report this, and clipping it was how the half naming
        // the campus system went missing.
        text: message,
      });
    },
  });

  // The exact question again, rather than asking the student to retype it.
  // `append` and `getState` are the runtime's own API; nothing here reconstructs
  // a message the student did not send.
  function retryLastQuestion() {
    const messages = runtime.thread.getState().messages;
    const lastUser = [...messages].reverse().find((message) => message.role === "user");
    const text = (lastUser?.content ?? [])
      .flatMap((part) => (part.type === "text" ? [part.text] : []))
      .join("\n")
      .trim();
    setStreamFailure(null);
    if (text) runtime.thread.append(text);
  }

  useEffect(() => {
    if (!cancelRef) return;
    cancelRef.current = cancelCurrentRun;
    return () => {
      cancelRef.current = null;
    };
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

  async function cancelCurrentRun() {
    const runId = telemetry.requestStop();
    // Keep the UI connection until the queue handshake arrives. Aborting it
    // earlier would lose the identifier needed to cancel the accepted run.
    if (!runId) return;
    try {
      await jsonFetch(`/api/chat/runs/${runId}/cancel`, { method: "POST" });
      if (telemetry.isCurrentRun(runId)) runtime.thread.cancelRun();
    } catch (error) {
      const requestId = requestIdOf(error) ?? telemetry.currentRequestId();
      captureProductEvent("chat.cancel", {
        result: "failed",
        run_id: runId,
        request_id: requestId,
      });
      captureRequestFailure(error, { operation: "chat.cancel", kind: "mutation" });
      toast.error(error instanceof Error ? error.message : "Unable to stop the run");
    } finally {
      telemetry.finishStop(runId);
    }
  }

  const isEmail = requirement?.tool === "send_email";
  const actionTitle = isEmail
    ? pick({ tr: "Bu e-posta senin adına gönderilecek", en: "This email will be sent as you" })
    : pick({ tr: "Bu işlem için onayın gerekiyor", en: "This action needs your approval" });
  // The one thing a consent dialog owes the student is the content they are
  // consenting to, in their own language. It used to be `Object.entries` of the
  // raw tool arguments, keyed by internal field names and cased by CSS — which
  // under lang="tr" rendered a key beginning with "i" as "İ".
  const confirmationFields = requirement?.arguments && typeof requirement.arguments === "object"
    ? activityFields(requirement.arguments as Record<string, unknown>)
    : [];

  return (
    <>
      <AssistantRuntimeProvider runtime={runtime}>
        <RunStateWatcher onChange={onRunningChange} />
        <Thread
          onCancelRun={() => void cancelCurrentRun()}
          notice={
            <>
              <CampusStatusNotice />
              {streamFailure ? (
                <div
                  role="alert"
                  data-slot="chat-stream-failure"
                  className="border-destructive/40 bg-destructive/10 text-destructive flex flex-wrap items-start gap-x-3 gap-y-2 rounded-xl border px-3 py-2.5 text-xs leading-5"
                >
                  <OctagonAlertIcon className="mt-0.5 size-4 shrink-0" aria-hidden />
                  <div className="min-w-0 flex-1">
                    <p className="font-medium">
                      {streamFailure.kind === "busy"
                        ? pick({ tr: "Önceki yanıt hâlâ hazırlanıyor", en: "The previous answer is still being written" })
                        : streamFailure.kind === "network"
                          ? pick({ tr: "Bağlantı koptu, yanıt tamamlanamadı", en: "The connection dropped before the answer finished" })
                          : pick({ tr: "Yanıt tamamlanamadı", en: "The answer could not be finished" })}
                    </p>
                    <p className="mt-0.5">
                      {streamFailure.kind === "busy"
                        ? pick({ tr: "Bitmesini bekle ya da yukarıdaki durdur düğmesiyle iptal et.", en: "Wait for it to finish, or stop it with the button above." })
                        : pick({
                            tr: "Sorunu tekrar sorabilirsin; yazdıkların kaybolmadı.",
                            en: "You can ask again; nothing you typed was lost.",
                          })}
                    </p>
                    <p className="mt-1 break-words opacity-80">{streamFailure.text}</p>
                  </div>
                  {streamFailure.kind === "busy" ? null : (
                    <Button
                      size="sm"
                      variant="default"
                      className="min-h-9 shrink-0 rounded-full px-3.5"
                      onClick={retryLastQuestion}
                    >
                      <RotateCcwIcon className="size-3.5" />
                      {pick({ tr: "Tekrar dene", en: "Try again" })}
                    </Button>
                  )}
                  <Button
                    size="icon"
                    variant="ghost"
                    className="text-destructive size-9 shrink-0 rounded-full"
                    onClick={() => setStreamFailure(null)}
                    aria-label={pick({ tr: "Uyarıyı kapat", en: "Dismiss" })}
                  >
                    <XIcon className="size-4" />
                  </Button>
                </div>
              ) : null}
            </>
          }
        />
      </AssistantRuntimeProvider>
      <AlertDialog open={Boolean(pendingConfirmation)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{actionTitle}</AlertDialogTitle>
            <AlertDialogDescription>
              {isEmail
                ? pick({
                    tr: "Alıcıyı, konuyu ve mesajı oku. Onaylamazsan hiçbir şey gönderilmez.",
                    en: "Read the recipient, subject and message. Nothing is sent unless you approve.",
                  })
                : pick({
                    tr: "Yapılacak işlemi ve bilgileri kontrol edip onayla.",
                    en: "Please review the action details before confirming.",
                  })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="max-h-72 overflow-auto rounded-lg border bg-muted/40 text-sm">
            {confirmationFields.length ? (
              <dl className="divide-border/60 divide-y">
                {confirmationFields.map((field) => (
                  <div
                    key={field.key}
                    className={cn(
                      "grid gap-1 px-3 py-2 sm:gap-3",
                      // The message body is the part that is actually read, so it
                      // gets its own full-width block instead of a narrow column.
                      field.key === "body" || field.key === "body_html"
                        ? "grid-cols-1"
                        : "sm:grid-cols-[6.5rem_minmax(0,1fr)]",
                    )}
                  >
                    <dt className="text-muted-foreground text-xs">{pick(field.label)}</dt>
                    <dd className="text-foreground/90 break-words whitespace-pre-wrap">{field.value}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="text-muted-foreground px-3 py-2 text-xs">
                {pick({
                  tr: "Bu işlemin gösterilecek bir ayrıntısı yok.",
                  en: "This action carries no details to show.",
                })}
              </p>
            )}
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={confirmationPending} onClick={() => void resolveConfirmation(false)}>
              {pick({ tr: "Vazgeç", en: "Cancel" })}
            </AlertDialogCancel>
            <AlertDialogAction disabled={confirmationPending} onClick={() => void resolveConfirmation(true)}>
              {confirmationPending ? <Loader2Icon className="animate-spin" /> : null}
              {isEmail
                ? pick({ tr: "Gönder", en: "Send" })
                : pick({ tr: "Onayla", en: "Approve" })}
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
  const { sessions, isLoading: sessionsLoading, isFetching: sessionsFetching, error: sessionsError, remove, removeAll, refetch } = useChatSessions();
  const [threadId, setThreadId] = useState<string | undefined>(undefined);
  const [selectedSessionId, setSelectedSessionId] = useState<string | undefined>(undefined);
  const [seedMessages, setSeedMessages] = useState<UIMessage[] | undefined>(undefined);
  const [chatKey, setChatKey] = useState(0);
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  const [confirmDeleteAll, setConfirmDeleteAll] = useState(false);
  const [mobileHistoryOpen, setMobileHistoryOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const sidebarPanelRef = useRef<PanelImperativeHandle | null>(null);
  const [runInFlight, setRunInFlight] = useState(false);
  const cancelRunRef = useRef<(() => Promise<void>) | null>(null);
  // A switch the student asked for while an answer was streaming, held until
  // they say what should happen to that answer.
  const [pendingSwitch, setPendingSwitch] = useState<{ run: () => void } | null>(null);

  /**
   * Run a thread switch, or ask first when it would throw away a live answer.
   *
   * Both switches remount the thread, and the run keeps going on the server, so
   * doing this silently spent the student's wait and then discarded the result.
   */
  function guardThreadSwitch(action: () => void) {
    if (runInFlight) {
      setPendingSwitch({ run: action });
      return;
    }
    action();
  }

  async function confirmPendingSwitch() {
    const pending = pendingSwitch;
    setPendingSwitch(null);
    if (!pending) return;
    // Cancel before switching: an abandoned run still costs campus requests.
    await cancelRunRef.current?.();
    pending.run();
  }
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

  async function deleteAllSessions() {
    try {
      const { deleted } = await removeAll.mutateAsync();
      newChat();
      setConfirmDeleteAll(false);
      captureProductEvent("chat_deleted_all", { count: deleted });
      toast.success(pick({ tr: `${deleted} sohbet silindi.`, en: `${deleted} chats deleted.` }));
    } catch (error) {
      captureError(error, { source: "chat_delete_all_sessions" });
      toast.error(error instanceof Error ? error.message : "Could not delete chats");
    }
  }

  const assistantThread = (
    <AssistantThread
      key={chatKey}
      threadId={threadId}
      initialMessages={seedMessages}
      onRunningChange={setRunInFlight}
      cancelRef={cancelRunRef}
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
              isLoading={sessionsLoading}
              retrying={sessionsFetching}
              error={sessionsError}
              onRetry={refetch}
              activeId={selectedSessionId}
              onNewChat={() => guardThreadSwitch(startNewChat)}
              onSelect={(sessionId) => guardThreadSwitch(() => void selectSession(sessionId))}
              onDelete={requestDelete}
              onDeleteAll={() => setConfirmDeleteAll(true)}
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
            isLoading={sessionsLoading}
            retrying={sessionsFetching}
            error={sessionsError}
            onRetry={refetch}
            activeId={selectedSessionId}
            onNewChat={() => { guardThreadSwitch(startNewChat); setMobileHistoryOpen(false); }}
            onSelect={(sessionId) => { guardThreadSwitch(() => void selectSession(sessionId)); setMobileHistoryOpen(false); }}
            onDelete={(sessionId) => {
              requestDelete(sessionId);
              setMobileHistoryOpen(false);
            }}
            onDeleteAll={() => { setConfirmDeleteAll(true); setMobileHistoryOpen(false); }}
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
      <AlertDialog
        open={confirmDeleteAll}
        onOpenChange={(open) => {
          if (!open && !removeAll.isPending) setConfirmDeleteAll(false);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogMedia className="bg-destructive/10 text-destructive">
              <Trash2Icon />
            </AlertDialogMedia>
            <AlertDialogTitle>
              {pick({ tr: "Tüm sohbetler silinsin mi?", en: "Delete all chats?" })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {pick({
                tr: `${sessions.length} sohbetin tamamı kalıcı olarak silinecek. Bu işlem geri alınamaz.`,
                en: `All ${sessions.length} of your chats will be permanently deleted. This action cannot be undone.`,
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={removeAll.isPending}>
              {pick({ tr: "Vazgeç", en: "Cancel" })}
            </AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={removeAll.isPending || !sessions.length}
              onClick={() => void deleteAllSessions()}
            >
              {removeAll.isPending ? <Loader2Icon className="animate-spin" /> : <Trash2Icon />}
              {pick({ tr: "Hepsini sil", en: "Delete all" })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Asked, not assumed. Tapping the sidebar while an answer streams is what
          an impatient student does, and it used to discard that answer without a
          word while the run kept costing campus requests on the server. */}
      <AlertDialog
        open={Boolean(pendingSwitch)}
        onOpenChange={(open) => {
          if (!open) setPendingSwitch(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogMedia className="bg-primary/10 text-primary">
              <Loader2Icon className="animate-spin motion-reduce:animate-none" />
            </AlertDialogMedia>
            <AlertDialogTitle>
              {pick({ tr: "Yanıt hâlâ hazırlanıyor", en: "The answer is still being written" })}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {pick({
                tr: "Şimdi başka bir sohbete geçersen bu yanıt yarıda kalır ve kaydedilmez.",
                en: "Switching now leaves this answer unfinished, and it will not be saved.",
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>
              {pick({ tr: "Beklemeye devam et", en: "Keep waiting" })}
            </AlertDialogCancel>
            <AlertDialogAction onClick={() => void confirmPendingSwitch()}>
              {pick({ tr: "Yanıtı bırak ve geç", en: "Leave it and switch" })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
