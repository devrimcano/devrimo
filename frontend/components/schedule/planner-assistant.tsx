"use client";

import { useRef, useState } from "react";
import { ChevronDownIcon, Loader2Icon, SendIcon, SparklesIcon } from "lucide-react";
import { useLocale } from "@/components/locale-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { newId } from "@/lib/new-id";

/**
 * Ask the assistant about the schedule being built, without leaving the page.
 *
 * Deliberately not the full chat runtime: this is one question and one answer,
 * so it reads the same `/api/chat` stream directly and keeps no history. The
 * assistant already receives the student's planned timetable with every turn,
 * so "is my Tuesday free" is answered about the grid above rather than about
 * whatever SAIS last registered them for.
 */
export function PlannerAssistant({ className }: { className?: string }) {
  const { pick } = useLocale();
  const t = (tr: string, en: string) => pick({ tr, en });
  const [open, setOpen] = useState(false);
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState("");
  const abortRef = useRef<AbortController | null>(null);

  const examples = [
    t("Bu programda çakışma var mı?", "Are there any conflicts in this schedule?"),
    t("Salı günüm boş mu?", "Is my Tuesday free?"),
    t("Kaç kredi almış oluyorum?", "How many credits am I taking?"),
    t("Bu döneme bir seçmeli daha sığar mı?", "Can I fit one more elective this term?"),
  ];

  async function ask(text: string) {
    const trimmed = text.trim();
    if (!trimmed || busy) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setBusy(true);
    setAnswer("");
    setFailed("");
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: controller.signal,
        body: JSON.stringify({
          messages: [{ id: newId(), role: "user", parts: [{ type: "text", text: trimmed }] }],
        }),
      });
      if (!response.ok || !response.body) throw new Error(`Request failed (${response.status})`);

      // The route writes AI SDK stream parts as server-sent events. Only the
      // text matters here; tool and confirmation parts belong to the full chat.
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let streamed = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (!payload || payload === "[DONE]") continue;
          try {
            const part = JSON.parse(payload) as { type?: string; delta?: string; errorText?: string };
            if (part.type === "text-delta" && part.delta) {
              streamed += part.delta;
              setAnswer(streamed);
            } else if (part.type === "error" && part.errorText) {
              setFailed(part.errorText);
            }
          } catch {
            // A partial or unrecognised part is not worth failing the answer for.
          }
        }
      }
      if (!streamed) setFailed((current) => current || t("Yanıt alınamadı.", "No answer came back."));
    } catch (error) {
      if ((error as Error)?.name === "AbortError") return;
      setFailed(error instanceof Error ? error.message : t("Bir şeyler ters gitti.", "Something went wrong."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card className={cn("overflow-hidden", className)}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left"
      >
        <SparklesIcon className="size-4 shrink-0 text-primary" />
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-semibold">{t("Programını AI'ya sor", "Ask AI about your schedule")}</span>
          <span className="block truncate text-[11px] text-muted-foreground">
            {t("Yukarıdaki programı görüyor; çakışma, boş gün ve kredi sorabilirsin.", "It can see the schedule above — ask about conflicts, free days or credits.")}
          </span>
        </span>
        <ChevronDownIcon className={cn("size-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-180")} />
      </button>
      {open ? (
        <CardContent className="grid grid-cols-[minmax(0,1fr)] gap-3 border-t pt-4">
          <div className="flex flex-wrap gap-1.5">
            {examples.map((example) => (
              <Button
                key={example}
                variant="outline"
                size="sm"
                className="h-7 rounded-full text-xs font-normal"
                disabled={busy}
                onClick={() => { setQuestion(example); void ask(example); }}
              >
                {example}
              </Button>
            ))}
          </div>
          <div className="flex items-end gap-2">
            <Textarea
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void ask(question); }
              }}
              rows={2}
              className="min-h-0 resize-none"
              placeholder={t("Programın hakkında bir şey sor…", "Ask something about your schedule…")}
              aria-label={t("Programın hakkında soru", "Question about your schedule")}
            />
            <Button className="shrink-0" disabled={busy || !question.trim()} onClick={() => void ask(question)} aria-label={t("Sor", "Ask")}>
              {busy ? <Loader2Icon className="animate-spin" /> : <SendIcon />}
            </Button>
          </div>
          {answer ? (
            <div className="whitespace-pre-wrap break-words rounded-xl border bg-muted/30 p-3 text-sm leading-6">{answer}</div>
          ) : null}
          {failed ? (
            <p className="rounded-lg border border-destructive/30 bg-destructive/5 p-2 text-xs leading-5 text-destructive">{failed}</p>
          ) : null}
          <p className="text-[11px] leading-4 text-muted-foreground">
            {t("Tek soruluk hızlı yanıt. Geçmişi olan bir sohbet için Sohbet sekmesini kullan.", "A quick one-off answer. Use the Chat tab for a conversation with history.")}
          </p>
        </CardContent>
      ) : null}
    </Card>
  );
}
