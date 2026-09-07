"use client";

import { unstable_useComposerInput } from "@assistant-ui/react";
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import {
  BookOpenCheckIcon,
  CalendarDaysIcon,
  LibraryIcon,
  RouteIcon,
  ShieldCheckIcon,
  SparklesIcon,
} from "lucide-react";
import { useLocale } from "@/components/locale-provider";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { jsonFetch } from "@/lib/api/fetcher";
import { formatUpdateCategory, formatUpdateSource } from "@/lib/campus";
import type { CampusUpdates } from "@/lib/types";

const suggestions = {
  tr: [
    { label: "Haftalık plan", prompt: "Programımdaki boşluklara göre bu hafta için gerçekçi bir çalışma planı yap", icon: CalendarDaysIcon },
    { label: "Akademik takvim", prompt: "Bu dönem yaklaşan önemli akademik tarihleri sırala", icon: BookOpenCheckIcon },
    { label: "Ders seçimi", prompt: "CENG 334 için ön koşulları ve açılan şubeleri karşılaştır", icon: LibraryIcon },
    { label: "Kampüs yaşamı", prompt: "Bu akşam sessiz çalışabileceğim kampüs seçeneklerini öner", icon: RouteIcon },
  ],
  en: [
    { label: "Weekly plan", prompt: "Build a realistic study plan around the gaps in my schedule this week", icon: CalendarDaysIcon },
    { label: "Academic calendar", prompt: "List the important academic dates coming up this semester", icon: BookOpenCheckIcon },
    { label: "Course planning", prompt: "Compare the prerequisites and available sections for CENG 334", icon: LibraryIcon },
    { label: "Campus life", prompt: "Suggest quiet places on campus where I can study tonight", icon: RouteIcon },
  ],
};

export function MetuWelcome() {
  const { pick } = useLocale();
  return (
    <div className="relative mx-auto mb-5 flex max-w-2xl flex-col items-center px-3 pt-5 text-center sm:mb-7 sm:px-4 sm:pt-0">
      <h1 className="motion-enter text-balance text-[2rem] leading-[1.08] font-semibold tracking-[-0.035em] sm:text-4xl">
        {pick({ tr: "Bugün neyi kolaylaştıralım?", en: "What should we make easier today?" })}
      </h1>
      <p className="motion-enter mt-3 max-w-xl text-sm leading-6 text-muted-foreground [animation-delay:55ms]">
        {pick({
          tr: "Ders programını düzenle, yaklaşan tarihleri yakala ve kampüs yaşamını tek sohbetten planla.",
          en: "Arrange your schedule, catch upcoming dates, and plan campus life in one conversation.",
        })}
      </p>
      <p className="motion-enter mt-2 flex items-center justify-center gap-1.5 text-xs leading-5 text-muted-foreground [animation-delay:90ms]">
        <ShieldCheckIcon className="size-3.5 shrink-0 text-primary" />
        {pick({ tr: "ODTÜClass ve e-posta yalnızca izin verdiğinde kullanılır.", en: "ODTÜClass and email are used only with your permission." })}
      </p>
    </div>
  );
}

function CampusDigest() {
  const { locale, pick } = useLocale();
  const query = useQuery({ queryKey: ["student", "updates", "digest"], queryFn: () => jsonFetch<CampusUpdates>("/api/student/updates?digest=true&limit=3"), staleTime: 5 * 60_000 });
  if (query.isLoading) return <section className="mt-6 w-full border-t pt-5" aria-label={pick({ tr: "Kampüs özeti yükleniyor", en: "Loading campus digest" })}><div className="mb-3 flex items-center justify-between"><Skeleton className="h-4 w-36" /><Skeleton className="h-4 w-24" /></div><div className="grid gap-3 sm:grid-cols-3"><Skeleton className="h-24 rounded-xl" /><Skeleton className="hidden h-24 rounded-xl sm:block" /><Skeleton className="hidden h-24 rounded-xl sm:block" /></div></section>;
  if (!query.data?.items.length) return null;
  return <section className="motion-enter mt-6 w-full border-t pt-5 text-left [animation-delay:160ms]" aria-label={pick({ tr: "Kampüs özeti", en: "Campus digest" })}><div className="mb-3 flex items-center justify-between gap-3"><p className="flex items-center gap-2 text-sm font-semibold"><SparklesIcon className="size-4 text-primary" />{pick({ tr: "Kampüsten son gelişmeler", en: "Latest from campus" })}</p><Link href="/updates" className="rounded-md text-xs font-medium text-primary underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">{pick({ tr: "Tümünü gör", en: "View all" })}</Link></div><div className="grid auto-cols-[minmax(15rem,82vw)] grid-flow-col gap-2 overflow-x-auto pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:grid-flow-row sm:grid-cols-3 sm:overflow-visible">{query.data.items.slice(0, 3).map((item) => <Link key={item.id} href={item.url || "/updates"} className="group rounded-xl border border-border/75 bg-card/55 p-3 transition-[border-color,background-color,transform] hover:-translate-y-0.5 hover:border-primary/25 hover:bg-card focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"><Badge variant="outline" className="mb-2 text-[10px]">{formatUpdateCategory(item.type, locale)}</Badge><p className="line-clamp-2 text-sm font-medium leading-5 group-hover:text-primary">{item.title}</p><p className="mt-1 line-clamp-1 text-[11px] text-muted-foreground">{formatUpdateSource(item.source)}</p></Link>)}</div></section>;
}

export function MetuStarterPrompts() {
  const { setText, send, isDisabled } = unstable_useComposerInput();
  const { locale, pick } = useLocale();

  return (
    <div className="w-full pb-1">
      <section aria-label={pick({ tr: "Örnek sorular", en: "Example questions" })}>
      <p className="mb-2 px-1 text-xs font-medium text-muted-foreground">{pick({ tr: "Hızlı başlangıç", en: "Quick start" })}</p>
      <div className="grid w-full auto-cols-[minmax(16rem,84vw)] grid-flow-col gap-2 overflow-x-auto pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:grid-flow-row sm:grid-cols-2 sm:overflow-visible">
        {suggestions[locale].map(({ label, prompt, icon: Icon }, index) => (
          <button
            key={prompt}
            type="button"
            disabled={isDisabled}
            className="motion-enter group flex min-h-[4.25rem] items-start gap-3 rounded-xl border border-border/70 bg-card/70 p-3 text-left transition-[transform,border-color,background-color] hover:-translate-y-0.5 hover:border-primary/25 hover:bg-card focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50"
            style={{ animationDelay: `${180 + index * 45}ms` }}
            onClick={() => { setText(prompt); send(); }}
          >
            <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground transition-colors group-hover:bg-primary/10 group-hover:text-primary"><Icon className="size-4" /></span>
            <span className="min-w-0">
              <span className="block text-[11px] font-semibold tracking-[0.08em] text-primary uppercase">{label}</span>
              <span className="mt-1 block text-sm leading-5 text-foreground/90">{prompt}</span>
            </span>
          </button>
        ))}
      </div>
      </section>
      <CampusDigest />
    </div>
  );
}
