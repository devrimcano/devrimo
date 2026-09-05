"use client";

import { useCallback, useEffect, useState } from "react";
import { ChevronLeftIcon, ChevronRightIcon, PlayIcon, XIcon } from "lucide-react";
import { useLocale } from "@/components/locale-provider";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const DISMISSED_KEY = "devrimo:schedule:v1:intro-dismissed";
const HIGHLIGHT = ["ring-2", "ring-primary", "ring-offset-2", "ring-offset-background", "rounded-xl"];

/**
 * A walkthrough of the planner that points at the actual screen.
 *
 * A list of paragraphs describing five features is a thing people scroll past.
 * This highlights the real control being described and scrolls it into view, so
 * the explanation and the thing explained are never more than a glance apart.
 * Each step names an element by `data-tour`; a step whose element is not on the
 * page is still shown, it simply lights nothing.
 */
export function PlannerIntro({ className }: { className?: string }) {
  const { pick } = useLocale();
  const t = (tr: string, en: string) => pick({ tr, en });
  const [running, setRunning] = useState(false);
  const [step, setStep] = useState(0);
  const [seen, setSeen] = useState(true);

  const steps: { target: string; title: string; body: string }[] = [
    {
      target: "pool",
      title: t("1 · Ders havuzu", "1 · Your course pool"),
      body: t(
        "“Almam gereken dersleri getir” müfredatından bu dönem alman gerekenleri çeker. Buradaki liste, programın kurulacağı havuzdur.",
        "“Load required courses” pulls what your curriculum says you still need. This list is the pool your schedule gets built from.",
      ),
    },
    {
      target: "search",
      title: t("2 · Ders ara ve ekle", "2 · Find and add a course"),
      body: t(
        "Ders kodu (PHYS213) ya da adı (termodinamik, sinyal) yaz. Altta çıkan öneriden seç; ders adı ve kredisiyle havuza eklenir. Seçmeli arıyorsan bölüm kodu yaz: HIST bütün tarih derslerini listeler.",
        "Type a code (PHYS213) or a name (thermodynamics, signals). Pick from the suggestions and it joins the pool with its real title and credits. Looking for an elective? Type a department: HIST lists every history course.",
      ),
    },
    {
      target: "pool",
      title: t("3 · Şubeler ve kırmızı ünlem", "3 · Sections and the red warning"),
      body: t(
        "Havuzdaki bir dersin üstüne tıkla: şubeleri hoca, gün, saat ve derslikle açılır. Kırmızı ünlem o şubeye kayıt olamayacağın demek — üstüne gelince hangi kısıta takıldığını söyler: bölüm, soyadı aralığı, sınıf ya da dersten aldığın not.",
        "Click a course in the pool to open its sections with instructor, day, time and room. A red warning means you cannot register for that section — hover it to see which rule blocks you: department, surname range, year, or the grade you already hold.",
      ),
    },
    {
      target: "rules",
      title: t("4 · Kuralların", "4 · Your rules"),
      body: t(
        "Boş kalsın istediğin günleri seç ve çakışmaları engelle. Bu anahtar kapalıyken uygun olmayan şubeler programa hiç girmez; elle eklemeye çalışsan da reddedilir.",
        "Choose the days you want kept free and whether to avoid conflicts. While this switch is off, sections you are not eligible for never enter the schedule — even if you try to add one by hand.",
      ),
    },
    {
      target: "manual",
      title: t("5 · Elle ekleme", "5 · Adding by hand"),
      body: t(
        "Katalogda olmayan bir şey mi var — staj, iş, antrenman? Buradan elle ekle. Gerçek bir ders ve şube yazarsan kısıtlar yine kontrol edilir.",
        "Something the catalog does not know about — work, training, an internship? Add it here. If you type a real course and section, its restrictions are still checked.",
      ),
    },
    {
      target: "build",
      title: t("6 · Programı üret", "6 · Build the schedule"),
      body: t(
        "Havuzdaki derslerden haftalık program çıkarır. Birden fazla düzen mümkünse alternatifler arasında gezebilir, beğendiğini favorileyebilirsin.",
        "Turns your pool into a week. When more than one arrangement works you can step through the alternatives and favorite the one you like.",
      ),
    },
    {
      target: "grid",
      title: t("7 · Program", "7 · The grid"),
      body: t(
        "Bir dersin herhangi bir saatine tıklamak o şubenin tüm saatlerini kaldırır. Çakışmalar kırmızı çerçeveyle işaretlenir.",
        "Clicking any hour of a course removes that whole section, not just the hour you clicked. Conflicts are outlined in red.",
      ),
    },
    {
      target: "assistant",
      title: t("8 · AI'ya sor", "8 · Ask the AI"),
      body: t(
        "Bu kutu yukarıdaki programı görüyor. “Salı günüm boş mu”, “çakışma var mı”, “kaç kredi” diye sorabilirsin.",
        "This box can see the schedule above. Ask it “is my Tuesday free”, “are there conflicts”, “how many credits”.",
      ),
    },
  ];

  useEffect(() => {
    // Deferred like every other localStorage read here: reading during render
    // makes the server and client markup disagree.
    const timer = window.setTimeout(() => {
      let done = false;
      try { done = window.localStorage.getItem(DISMISSED_KEY) === "1"; } catch { /* storage blocked */ }
      setSeen(done);
      setRunning(!done);
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  const spotlight = useCallback((target: string | null) => {
    for (const node of Array.from(document.querySelectorAll("[data-tour]"))) {
      node.classList.remove(...HIGHLIGHT);
    }
    if (!target) return;
    const node = document.querySelector('[data-tour="' + target + '"]');
    if (!node) return;
    node.classList.add(...HIGHLIGHT);
    // "nearest" leaves a target that is already on screen exactly where it
    // is, so stepping through does not yank the page under the reader.
    node.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
  }, []);

  const currentTarget = steps[step]?.target ?? null;
  useEffect(() => {
    if (!running) {
      spotlight(null);
      return;
    }
    spotlight(currentTarget);
    return () => spotlight(null);
  }, [running, currentTarget, spotlight]);

  function finish() {
    setRunning(false);
    setSeen(true);
    setStep(0);
    spotlight(null);
    try { window.localStorage.setItem(DISMISSED_KEY, "1"); } catch { /* storage blocked */ }
  }

  if (!running) {
    return (
      <button
        type="button"
        onClick={() => { setStep(0); setRunning(true); }}
        className={cn("inline-flex items-center gap-1.5 text-xs text-muted-foreground underline underline-offset-4 hover:text-foreground", className)}
      >
        <PlayIcon className="size-3" />
        {seen ? t("Bu ekran nasıl kullanılır?", "How do I use this screen?") : t("Tanıtımı başlat", "Start the walkthrough")}
      </button>
    );
  }

  const current = steps[step];
  // Plain document flow — never fixed, never sticky. Floating at the
  // bottom-right it sat squarely on the timetable's afternoon slots and the
  // export buttons, so the walkthrough explaining the grid was the thing
  // stopping anyone from clicking it. Nothing on this screen is worth covering
  // to save a student one scroll.
  return (
    <div
      role="region"
      aria-live="polite"
      aria-label={t("Program ekranı tanıtımı", "Planner walkthrough")}
      className={cn("rounded-2xl border bg-muted/30 p-4", className)}
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold">{current.title}</p>
          <p className="mt-1 break-words text-xs leading-5 text-muted-foreground">{current.body}</p>
        </div>
        <Button size="icon-sm" variant="ghost" onClick={finish} aria-label={t("Tanıtımı kapat", "Close walkthrough")}>
          <XIcon />
        </Button>
      </div>
      <div className="mt-3 flex items-center gap-2">
        <div className="flex flex-1 items-center gap-1" aria-hidden="true">
          {steps.map((item, index) => (
            <span key={item.title} className={cn("h-1 flex-1 rounded-full transition-colors", index <= step ? "bg-primary" : "bg-muted")} />
          ))}
        </div>
        <span className="shrink-0 text-[11px] tabular-nums text-muted-foreground">{step + 1}/{steps.length}</span>
      </div>
      <div className="mt-3 flex items-center justify-between gap-2">
        <Button variant="ghost" size="sm" disabled={step === 0} onClick={() => setStep((value) => Math.max(0, value - 1))}>
          <ChevronLeftIcon />{t("Geri", "Back")}
        </Button>
        {step === steps.length - 1 ? (
          <Button size="sm" onClick={finish}>{t("Anladım", "Got it")}</Button>
        ) : (
          <Button size="sm" onClick={() => setStep((value) => Math.min(steps.length - 1, value + 1))}>
            {t("Devam", "Next")}<ChevronRightIcon />
          </Button>
        )}
      </div>
    </div>
  );
}
