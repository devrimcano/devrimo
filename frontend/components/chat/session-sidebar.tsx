"use client";

import {
  Loader2Icon,
  MessageSquareIcon,
  MessageSquarePlusIcon,
  PanelLeftCloseIcon,
  PanelLeftOpenIcon,
  RotateCcwIcon,
  Trash2Icon,
  TriangleAlertIcon,
} from "lucide-react";
import { useId } from "react";
import { motion, useReducedMotion } from "motion/react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";
import type { ChatSession } from "@/lib/types";
import { useLocale } from "@/components/locale-provider";

export function SessionSidebar({
  sessions,
  activeId,
  onNewChat,
  onSelect,
  onDelete,
  onDeleteAll,
  isLoading = false,
  retrying = false,
  error,
  onRetry,
  className,
  collapsed = false,
  onToggleCollapse,
}: {
  sessions: ChatSession[];
  activeId?: string;
  onNewChat: () => void;
  onSelect: (sessionId: string) => void;
  onDelete: (sessionId: string) => void;
  onDeleteAll?: () => void;
  isLoading?: boolean;
  retrying?: boolean;
  error?: unknown;
  onRetry?: () => void | Promise<unknown>;
  className?: string;
  collapsed?: boolean;
  onToggleCollapse?: () => void;
}) {
  const { pick } = useLocale();
  const instanceId = useId();
  const reduceMotion = useReducedMotion();
  return (
    <aside aria-label={pick({ tr: "Sohbet geçmişi", en: "Chat history" })} className={cn("motion-sidebar flex h-full min-w-0 flex-col bg-sidebar/88", className)}>
      <div className={cn("border-b p-3", collapsed && "flex flex-col items-center gap-2 px-2")}>
        {onToggleCollapse ? (
          <div className={cn("mb-2 flex items-center justify-between", collapsed && "mb-0")}>
            {!collapsed ? (
              <div>
                <p className="text-sm font-semibold">{pick({ tr: "Sohbetler", en: "Chats" })}</p>
                <p className="text-[11px] text-muted-foreground">{isLoading ? pick({ tr: "Yükleniyor…", en: "Loading…" }) : `${sessions.length} ${pick({ tr: "sohbet", en: sessions.length === 1 ? "conversation" : "conversations" })}`}</p>
              </div>
            ) : null}
            <Button variant="ghost" size="icon-sm" onClick={onToggleCollapse} aria-label={collapsed ? pick({ tr: "Sohbet kenar çubuğunu aç", en: "Expand chat sidebar" }) : pick({ tr: "Sohbet kenar çubuğunu daralt", en: "Collapse chat sidebar" })}>
              {collapsed ? <PanelLeftOpenIcon /> : <PanelLeftCloseIcon />}
            </Button>
          </div>
        ) : null}
        <Button className={cn("w-full justify-start", collapsed && "size-9 justify-center px-0")} variant="outline" onClick={onNewChat} aria-label={collapsed ? pick({ tr: "Yeni sohbet", en: "New chat" }) : undefined}>
          <MessageSquarePlusIcon />
          {!collapsed ? pick({ tr: "Yeni sohbet", en: "New chat" }) : null}
        </Button>
      </div>
      {!collapsed ? <ScrollArea className="min-h-0 flex-1">
        <nav aria-label={pick({ tr: "Sohbetler", en: "Chats" })} className="flex flex-col gap-1.5 px-2 pb-3 pt-1">
          {isLoading ? (
            <div className="flex items-center gap-2 px-2 py-6 text-sm text-muted-foreground" role="status">
              <Loader2Icon className="size-4 animate-spin" aria-hidden="true" />
              {pick({ tr: "Sohbet geçmişi yükleniyor…", en: "Loading chat history…" })}
            </div>
          ) : error ? (
            <div className="mx-1 my-2 rounded-xl border border-destructive/35 bg-destructive/5 p-3 text-sm" role="alert">
              <div className="flex items-start gap-2 text-destructive">
                <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
                <p>{pick({ tr: "Sohbet geçmişi yüklenemedi.", en: "Chat history could not be loaded." })}</p>
              </div>
              {onRetry ? (
                <Button variant="outline" size="sm" className="mt-3" onClick={() => void onRetry()} disabled={retrying}>
                  <RotateCcwIcon className={retrying ? "animate-spin" : undefined} />
                  {retrying ? pick({ tr: "Yenileniyor…", en: "Retrying…" }) : pick({ tr: "Tekrar dene", en: "Try again" })}
                </Button>
              ) : null}
            </div>
          ) : sessions.length === 0 ? (
            <p className="px-2 py-6 text-sm text-muted-foreground">{pick({ tr: "İlk sohbetini başlat.", en: "Start your first conversation." })}</p>
          ) : (
            sessions.map((session) => {
              const active = activeId === session.id;
              return (
                <div
                  key={session.id}
                  className={cn(
                    "group relative isolate flex min-h-12 items-center overflow-hidden rounded-xl transition-colors duration-300",
                    active ? "text-sidebar-foreground" : "text-muted-foreground hover:bg-card/45 hover:text-sidebar-foreground",
                  )}
                >
                  {active ? (
                    <motion.div
                      layoutId={`active-chat-surface-${instanceId}`}
                      className="absolute inset-0 -z-10 rounded-xl border border-white/70 bg-card/80 shadow-[0_1px_0_rgb(255_255_255/85%)_inset,0_8px_24px_rgb(65_45_36/9%)] backdrop-blur-xl dark:border-white/10 dark:bg-white/[0.065] dark:shadow-[0_1px_0_rgb(255_255_255/8%)_inset,0_10px_28px_rgb(0_0_0/24%)]"
                      transition={reduceMotion ? { duration: 0 } : { type: "spring", stiffness: 430, damping: 36, mass: 0.75 }}
                    />
                  ) : null}
                  <button
                    type="button"
                    className="relative z-10 flex min-w-0 flex-1 items-center gap-2.5 px-2.5 py-2 text-left text-sm outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                    onClick={() => onSelect(session.id)}
                    aria-current={active ? "page" : undefined}
                  >
                    <span className={cn("grid size-7 shrink-0 place-items-center rounded-lg transition-colors duration-300", active ? "bg-primary/10 text-primary" : "bg-sidebar-accent/55 text-muted-foreground group-hover:text-foreground")}>
                      <MessageSquareIcon className="size-3.5" />
                    </span>
                    <span className={cn("min-w-0 flex-1 truncate", active && "font-medium tracking-[-0.01em]")}>{session.title?.trim() || pick({ tr: "İsimsiz sohbet", en: "Untitled chat" })}</span>
                  </button>
                  <Button
                    variant="ghost"
                    size="icon-xs"
                    data-slot="session-delete"
                    className="relative z-10 mr-1.5 shrink-0 text-muted-foreground opacity-100 transition-[opacity,background-color,color] lg:opacity-0 lg:group-hover:opacity-100 lg:focus-visible:opacity-100"
                    onClick={() => onDelete(session.id)}
                    aria-label={pick({ tr: "Sohbeti sil", en: "Delete chat" })}
                  >
                    <Trash2Icon />
                  </Button>
                </div>
              );
            })
          )}
        </nav>
      </ScrollArea> : <div className="flex-1" />}
      {!collapsed ? (
        <div className="border-t px-4 py-3">
          {/* Only offered when there is something to delete, so the sidebar of
              a student with no history is not led by a destructive action. */}
          {onDeleteAll && sessions.length ? (
            <Button
              variant="ghost"
              size="sm"
              className="mb-2 h-8 w-full justify-start px-2 text-xs text-muted-foreground hover:text-destructive"
              onClick={onDeleteAll}
            >
              <Trash2Icon className="size-3.5" />
              {pick({ tr: "Tüm sohbetleri sil", en: "Delete all chats" })}
            </Button>
          ) : null}
          <p className="text-[11px] leading-4 text-muted-foreground">
            {pick({ tr: "Sohbet geçmişin yalnızca hesabına bağlıdır.", en: "Your chat history belongs only to your account." })}
          </p>
        </div>
      ) : null}
    </aside>
  );
}
