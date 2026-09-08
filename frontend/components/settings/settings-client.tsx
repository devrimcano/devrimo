"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  BrainIcon,
  CableIcon,
  ChevronRightIcon,
  Loader2Icon,
  RotateCcwIcon,
  ShieldCheckIcon,
  SlidersHorizontalIcon,
  SparklesIcon,
  Trash2Icon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { useLocale } from "@/components/locale-provider";
import { CampusConnectionCard } from "@/components/settings/campus-connection-card";
import { useProfile } from "@/hooks/useProfile";
import { useMemories } from "@/hooks/useMemories";
import { captureError, captureProductEvent } from "@/components/posthog-analytics";
import { jsonFetch } from "@/lib/api/fetcher";
import { AVAILABLE_TERMS, formatAcademicTerm } from "@/lib/campus";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { formatMetuDepartment } from "@/lib/metu-course-code";
import { SettingsQueryError } from "@/components/settings/query-state";

export function SettingsClient() {
  const { pick } = useLocale();
  const { profile, isLoading: profileLoading, isFetching: profileFetching, queryError: profileError, refetch: refetchProfile, update: updateProfile } = useProfile();
  const { memories, isLoading: memoriesLoading, isFetching: memoriesFetching, queryError: memoriesError, refetch: refetchMemories, clear: clearMemories } = useMemories();

  useEffect(() => {
    captureProductEvent("settings_opened", {});
  }, []);

  async function clearAllMemories() {
    try {
      await clearMemories.mutateAsync();
      toast.success(pick({ tr: "Hatırlanan tercihlerin silindi.", en: "Remembered preferences cleared." }));
    } catch (error) {
      captureError(error, { source: "settings_clear_memories" });
      toast.error(error instanceof Error ? error.message : pick({ tr: "Tercihler silinemedi.", en: "Preferences could not be cleared." }));
    }
  }

  async function reopenSetup() {
    try {
      await updateProfile.mutateAsync({ onboarding_completed: false, onboarding_step: "welcome" });
      toast.success(pick({ tr: "Kurulum adımları tekrar açıldı.", en: "Setup steps reopened." }));
    } catch (error) {
      captureError(error, { source: "settings_reopen_setup" });
      toast.error(error instanceof Error ? error.message : pick({ tr: "Kurulum açılamadı.", en: "Setup could not be reopened." }));
    }
  }

  const navigation = [
    { href: "#connection", icon: CableIcon, label: pick({ tr: "ODTÜ bağlantısı", en: "METU connection" }) },
    { href: "#privacy", icon: ShieldCheckIcon, label: pick({ tr: "Veri erişimi", en: "Data access" }) },
    { href: "#personalization", icon: SparklesIcon, label: pick({ tr: "Kişiselleştirme", en: "Personalization" }) },
    { href: "#memory", icon: BrainIcon, label: pick({ tr: "Hatırlananlar", en: "Remembered items" }) },
    { href: "#setup", icon: RotateCcwIcon, label: pick({ tr: "Kurulum", en: "Setup" }) },
  ];

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-5 sm:px-6 sm:py-7 lg:px-8">
      <header className="motion-enter relative overflow-hidden rounded-3xl border bg-card/85 p-5 shadow-sm sm:p-7">
        <div className="absolute -right-12 -top-20 size-52 rounded-full bg-primary/8 blur-2xl" aria-hidden />
        <div className="relative flex max-w-3xl items-start gap-4">
          <span className="grid size-11 shrink-0 place-items-center rounded-2xl bg-primary text-primary-foreground shadow-[0_7px_0_#901129]"><SlidersHorizontalIcon className="size-5" /></span>
          <div>
            <Badge variant="outline" className="mb-3 border-primary/20 bg-primary/5 text-primary">{pick({ tr: "Kontrol sende", en: "You're in control" })}</Badge>
            <h1 className="text-2xl font-semibold tracking-[-0.035em] sm:text-3xl">{pick({ tr: "Ayarlar ve gizlilik", en: "Settings and privacy" })}</h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-muted-foreground">{pick({ tr: "ODTÜ bağlantını, hangi bilgi kaynaklarının kullanılabileceğini ve asistanın hatırladıklarını tek yerde yönet.", en: "Manage your METU connection, which information sources may be used, and what the assistant remembers—all in one place." })}</p>
          </div>
        </div>
      </header>

      <nav
        aria-label={pick({ tr: "Ayar bölümleri", en: "Settings sections" })}
        className="sticky top-16 z-20 -mx-4 mt-3 flex gap-2 overflow-x-auto border-y bg-background/95 px-4 py-2.5 shadow-sm [scrollbar-width:none] backdrop-blur [&::-webkit-scrollbar]:hidden lg:hidden"
      >
        {navigation.map(({ href, icon: Icon, label }) => (
          <a key={href} href={href} className="flex min-h-10 shrink-0 items-center gap-2 rounded-full border bg-card px-3 text-xs font-medium shadow-sm focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
            <Icon className="size-3.5 text-primary" />
            {label}
          </a>
        ))}
      </nav>

      <div className="mt-4 grid items-start gap-6 lg:mt-6 lg:grid-cols-[14rem_minmax(0,1fr)]">
        <aside className="hidden lg:sticky lg:top-6 lg:block">
          <nav aria-label={pick({ tr: "Ayar bölümleri", en: "Settings sections" })} className="rounded-2xl border bg-card/70 p-2 shadow-sm">
            {navigation.map(({ href, icon: Icon, label }) => (
              <a key={href} href={href} className="group flex min-h-11 items-center gap-3 rounded-xl px-3 text-sm font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
                <Icon className="size-4 text-primary" />
                <span className="flex-1">{label}</span>
                <ChevronRightIcon className="size-3.5 opacity-0 transition-opacity group-hover:opacity-60" />
              </a>
            ))}
          </nav>
          <div className="mt-3 rounded-2xl border border-primary/15 bg-primary/[0.035] p-4">
            <div className="flex items-center gap-2 text-sm font-semibold"><ShieldCheckIcon className="size-4 text-primary" />{pick({ tr: "Gizlilik özeti", en: "Privacy summary" })}</div>
            <p className="mt-2 text-xs leading-5 text-muted-foreground">{pick({ tr: "E-posta ve ODTÜClass isteğe bağlıdır. Şifren gösterilmez; içerikler yönetici ekranlarına taşınmaz.", en: "Email and ODTÜClass are optional. Your password is never shown, and contents never appear in admin views." })}</p>
          </div>
        </aside>

        <div className="min-w-0 space-y-5">
          <CampusConnectionCard />

          <Card id="personalization" className="motion-enter surface-raised scroll-mt-32 border-0 ring-1 ring-foreground/8 [animation-delay:40ms] lg:scroll-mt-24">
            <CardHeader className="border-b bg-muted/20"><div className="flex items-start gap-3"><span className="grid size-9 shrink-0 place-items-center rounded-xl bg-primary/10 text-primary"><SparklesIcon className="size-4" /></span><div><CardTitle>{pick({ tr: "Kişiselleştirme ve güncellemeler", en: "Personalization and updates" })}</CardTitle><CardDescription className="mt-1">{pick({ tr: "İlgi alanlarını düzenle ve e-postadan yalnızca yapılandırılmış tarih/etkinlik bilgisi çıkarılmasına izin ver.", en: "Edit your interests and choose whether email may yield structured date and event facts." })}</CardDescription></div></div></CardHeader>
            <CardContent className="space-y-5"><AcademicContextSummary /><AcademicDataManager /><PreferenceEditor /><div className="grid grid-cols-[1fr_auto] items-start gap-4 rounded-xl border bg-background/55 p-4"><div><Label htmlFor="mail-facts" className="font-semibold">{pick({ tr: "E-postadan yapılandırılmış güncellemeler", en: "Structured updates from email" })}</Label><p className="mt-1 text-sm leading-5 text-muted-foreground">{pick({ tr: "Etkinlik ve son tarihlerin yalnızca başlığı, özeti ve zamanı saklanır. E-posta gövdesi kaydedilmez.", en: "Only the title, summary, and time of events and deadlines are kept. Email bodies are not stored." })}</p></div>{profileLoading ? <Loader2Icon className="mt-1 size-4 animate-spin text-muted-foreground" aria-label={pick({ tr: "Yükleniyor", en: "Loading" })} /> : profileError ? <SettingsQueryError message={pick({ tr: "Profil ayarları yüklenemedi.", en: "Profile settings could not be loaded." })} retryLabel={pick({ tr: "Tekrar dene", en: "Try again" })} retryingLabel={pick({ tr: "Yenileniyor…", en: "Retrying…" })} retry={refetchProfile} retrying={profileFetching} /> : <Switch id="mail-facts" checked={profile?.mail_facts_enabled ?? false} disabled={!profile || updateProfile.isPending} onCheckedChange={(checked) => void updateProfile.mutateAsync({ mail_facts_enabled: checked }).catch((error) => toast.error(error instanceof Error ? error.message : "Update failed"))} />}</div></CardContent>
          </Card>

          <Card id="memory" className="motion-enter surface-raised scroll-mt-32 border-0 ring-1 ring-foreground/8 [animation-delay:70ms] lg:scroll-mt-24">
            <CardHeader className="border-b bg-muted/20">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="flex items-start gap-3">
                  <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-primary/10 text-primary"><BrainIcon className="size-4" /></span>
                  <div><CardTitle>{pick({ tr: "Hatırlanan tercihler", en: "Remembered preferences" })}</CardTitle><CardDescription className="mt-1">{pick({ tr: "Yalnızca açıkça hatırlamasını istediğin, hassas olmayan tercihler burada tutulur.", en: "Only non-sensitive preferences you explicitly asked the assistant to remember are kept here." })}</CardDescription></div>
                </div>
              {!memoriesLoading && !memoriesError ? <Badge variant="secondary">{memories.length} {pick({ tr: "kayıt", en: memories.length === 1 ? "item" : "items" })}</Badge> : null}
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              {memoriesLoading ? (
                <div className="flex min-h-28 items-center justify-center gap-2 text-sm text-muted-foreground"><Loader2Icon className="size-4 animate-spin" />{pick({ tr: "Hatırlananlar yükleniyor…", en: "Loading remembered items…" })}</div>
              ) : memoriesError ? (
                <SettingsQueryError
                  message={pick({ tr: "Hatırlanan tercihler yüklenemedi.", en: "Remembered preferences could not be loaded." })}
                  retryLabel={pick({ tr: "Tekrar dene", en: "Try again" })}
                  retryingLabel={pick({ tr: "Yenileniyor…", en: "Retrying…" })}
                  retry={refetchMemories}
                  retrying={memoriesFetching}
                />
              ) : memories.length ? (
                <ul className="grid gap-2 sm:grid-cols-2">
                  {memories.map((memory) => <li key={memory.id} className="rounded-xl border bg-background/55 px-4 py-3 text-sm leading-5">{memory.content}</li>)}
                </ul>
              ) : (
                <div className="flex min-h-28 flex-col items-center justify-center rounded-xl border border-dashed bg-muted/15 px-4 text-center">
                  <SparklesIcon className="size-5 text-primary" />
                  <p className="mt-2 text-sm font-medium">{pick({ tr: "Henüz hatırlanan bir tercih yok", en: "Nothing is remembered yet" })}</p>
                  <p className="mt-1 max-w-sm text-xs leading-5 text-muted-foreground">{pick({ tr: "Örneğin “Yanıtları kısa tut” dediğinde ve hatırlamasını istediğinde burada görünür.", en: "For example, an instruction such as “Keep answers concise” appears here when you ask it to remember." })}</p>
                </div>
              )}

              {memories.length ? (
                <AlertDialog>
                  <AlertDialogTrigger render={<Button variant="outline" className="text-destructive" />}><Trash2Icon />{pick({ tr: "Tümünü unuttur", en: "Forget everything" })}</AlertDialogTrigger>
                  <AlertDialogContent>
                    <AlertDialogHeader><AlertDialogTitle>{pick({ tr: "Tüm tercihler unutulsun mu?", en: "Forget all preferences?" })}</AlertDialogTitle><AlertDialogDescription>{pick({ tr: "Hatırlanan tercihler kalıcı olarak silinir. Sohbet geçmişin ve ODTÜ bağlantın değişmez.", en: "Remembered preferences are permanently deleted. Chat history and your METU connection are unchanged." })}</AlertDialogDescription></AlertDialogHeader>
                    <AlertDialogFooter><AlertDialogCancel>{pick({ tr: "Vazgeç", en: "Cancel" })}</AlertDialogCancel><AlertDialogAction variant="destructive" disabled={clearMemories.isPending} onClick={() => void clearAllMemories()}>{clearMemories.isPending ? <Loader2Icon className="animate-spin" /> : <Trash2Icon />}{pick({ tr: "Tümünü unuttur", en: "Forget everything" })}</AlertDialogAction></AlertDialogFooter>
                  </AlertDialogContent>
                </AlertDialog>
              ) : null}
            </CardContent>
          </Card>

          <Card id="setup" className="motion-enter surface-raised scroll-mt-32 border-0 ring-1 ring-foreground/8 [animation-delay:110ms] lg:scroll-mt-24">
            <CardHeader className="border-b bg-muted/20">
              <div className="flex items-start gap-3"><span className="grid size-9 shrink-0 place-items-center rounded-xl bg-muted text-muted-foreground"><RotateCcwIcon className="size-4" /></span><div><CardTitle>{pick({ tr: "Kurulumu yeniden gözden geçir", en: "Review setup again" })}</CardTitle><CardDescription className="mt-1">{pick({ tr: "Dil, hitap şekli, ODTÜ bağlantısı ve veri erişimi seçimlerini adım adım yeniden incele. Mevcut kayıtların silinmez.", en: "Review language, how you're addressed, your METU connection, and data-access choices step by step. Existing data is not deleted." })}</CardDescription></div></div>
            </CardHeader>
            <CardContent>
              <Button variant="outline" disabled={updateProfile.isPending} onClick={() => void reopenSetup()}>{updateProfile.isPending ? <Loader2Icon className="animate-spin" /> : <RotateCcwIcon />}{pick({ tr: "Kurulum adımlarını aç", en: "Open setup steps" })}</Button>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}

type PreferenceItem = { key: string; value: Record<string, unknown>; provenance: "explicit" | "learned"; confidence: number; updated_at: string };

type AcademicContext = { department: string | null; surname_prefix: string | null; degree_level: string | null; year_of_study: number | null; program_code: string | null; campus: string | null; source: string; verified_at: string | null; confirmed_at: string | null };

function AcademicContextSummary() {
  const { pick, locale } = useLocale();
  const query = useQuery({ queryKey: ["student", "context"], queryFn: () => jsonFetch<AcademicContext>("/api/student/context") });
  const context = query.data;
  const rows = [
    { key: "department", label: pick({ tr: "Bölüm", en: "Department" }), value: formatMetuDepartment(context?.program_code ?? null, context?.department ?? null, locale) },
    { key: "surname", label: pick({ tr: "Soyadı (ilk 2 harf)", en: "Surname (first 2 letters)" }), value: context?.surname_prefix },
    { key: "year", label: pick({ tr: "Sınıf", en: "Year of study" }), value: context?.year_of_study },
    { key: "program", label: pick({ tr: "Program kodu", en: "Program code" }), value: context?.program_code },
    { key: "campus", label: pick({ tr: "Kampüs", en: "Campus" }), value: context?.campus },
  ];

  return <section aria-labelledby="academic-context-heading" className="rounded-xl border bg-background/55 p-4">
    <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
      <div>
        <p id="academic-context-heading" className="font-semibold">{pick({ tr: "Akademik bağlam", en: "Academic context" })}</p>
        <p className="mt-0.5 max-w-xl text-xs leading-5 text-muted-foreground">{pick({ tr: "Ders planlayıcı ve sohbet bu bilgileri SAIS kaydından kullanır. Değişiklik için ODTÜ'den yeniden yenile.", en: "The planner and chat use these details from your SAIS record. Refresh from METU to update them." })}</p>
      </div>
      {query.isLoading ? <Loader2Icon className="size-4 animate-spin text-muted-foreground" aria-label={pick({ tr: "Yükleniyor", en: "Loading" })} /> : query.error ? null : context ? <Badge variant={context.verified_at ? "secondary" : "outline"}>SAIS</Badge> : null}
    </div>

    {query.isLoading ? <div className="flex min-h-20 items-center justify-center gap-2 text-sm text-muted-foreground"><Loader2Icon className="size-4 animate-spin" />{pick({ tr: "Akademik bağlam yükleniyor…", en: "Loading academic context…" })}</div> : query.error ? <SettingsQueryError
      message={pick({ tr: "Akademik bağlam yüklenemedi.", en: "Academic context could not be loaded." })}
      retryLabel={pick({ tr: "Tekrar dene", en: "Try again" })}
      retryingLabel={pick({ tr: "Yenileniyor…", en: "Retrying…" })}
      retry={() => query.refetch()}
      retrying={query.isFetching}
    /> : context ? <dl className="grid grid-cols-2 gap-x-5 gap-y-3 sm:grid-cols-3">
      {rows.map((row) => <div key={row.key} className={row.key === "department" ? "col-span-2 sm:col-span-1" : undefined}>
        <dt className="text-[11px] uppercase tracking-wide text-muted-foreground">{row.label}</dt>
        <dd className={row.value ? "text-sm font-medium" : "text-sm text-muted-foreground"}>{row.value || "—"}</dd>
      </div>)}
    </dl> : !query.isLoading ? <p className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-2 text-xs leading-5">{pick({ tr: "SAIS akademik bilgisi henüz alınmadı. Aşağıdaki dönemden ODTÜ verilerini yenile.", en: "SAIS academic details have not been fetched yet. Refresh METU data for a term below." })}</p> : null}
  </section>;
}

type AcademicData = {
  context: AcademicContext | null;
  snapshots: Array<{ term: string; completed_course_count: number; completed_course_codes: string[]; enrolled_course_count: number; fetched_at: string; source: string }>;
  has_cached_data: boolean;
};

function AcademicDataManager() {
  const { pick, locale } = useLocale();
  const client = useQueryClient();
  const [term, setTerm] = useState("20261");
  const [coursesExpanded, setCoursesExpanded] = useState(false);
  const query = useQuery({ queryKey: ["student", "academic-data"], queryFn: () => jsonFetch<AcademicData>("/api/student/academic-data") });
  const sync = useMutation({
    mutationFn: () => jsonFetch<AcademicData>("/api/student/academic-data/sync", { method: "POST", body: { term, force: true } }),
    onSuccess: (data) => {
      client.setQueryData(["student", "academic-data"], data);
      void client.invalidateQueries({ queryKey: ["student", "context"] });
      toast.success(pick({ tr: "Akademik veriler SAIS'ten yenilendi.", en: "Academic data refreshed from SAIS." }));
    },
    onError: (error) => {
      // The refresh writes its snapshot in its own transaction before the
      // reply is built, so a dropped or cancelled response does not mean the
      // data was not saved. Re-read rather than assume the worst.
      void client.invalidateQueries({ queryKey: ["student", "academic-data"] });
      void client.invalidateQueries({ queryKey: ["student", "context"] });
      toast.error(error.message);
    },
  });
  const reset = useMutation({
    mutationFn: () => jsonFetch<{ deleted: boolean }>("/api/student/academic-data", { method: "DELETE" }),
    onSuccess: () => {
      client.setQueryData(["student", "academic-data"], { context: null, snapshots: [], has_cached_data: false });
      void client.invalidateQueries({ queryKey: ["student", "context"] });
      toast.success(pick({ tr: "Bölüm ve transkript verileri silindi.", en: "Department and transcript data deleted." }));
    },
    onError: (error) => toast.error(error.message),
  });
  const latest = query.data?.snapshots[0];
  return <div className="rounded-xl border bg-background/55 p-4">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div><p className="font-semibold">{pick({ tr: "Kayıtlı akademik veriler", en: "Stored academic data" })}</p><p className="mt-1 max-w-2xl text-xs leading-5 text-muted-foreground">{pick({ tr: "Bölümün ve transkriptindeki ders kodları ilk kullanımda bir kez alınır. Notlar burada gösterilmez veya yapay zekâ belleğine eklenmez.", en: "Your department and transcript course codes are fetched once on first use. Grades are not shown here or added to AI memory." })}</p></div>
      {query.isLoading ? <Loader2Icon className="size-4 animate-spin text-muted-foreground" aria-label={pick({ tr: "Yükleniyor", en: "Loading" })} /> : query.error ? null : <Badge variant={query.data?.has_cached_data ? "secondary" : "outline"}>{query.data?.has_cached_data ? pick({ tr: "Kayıtlı", en: "Stored" }) : pick({ tr: "Henüz alınmadı", en: "Not fetched" })}</Badge>}
    </div>
    {query.isLoading ? <div className="flex min-h-20 items-center justify-center gap-2 text-sm text-muted-foreground"><Loader2Icon className="size-4 animate-spin" />{pick({ tr: "Akademik veriler yükleniyor…", en: "Loading academic data…" })}</div> : query.error ? <div className="mt-3"><SettingsQueryError
      message={pick({ tr: "Akademik veriler yüklenemedi.", en: "Academic data could not be loaded." })}
      retryLabel={pick({ tr: "Tekrar dene", en: "Try again" })}
      retryingLabel={pick({ tr: "Yenileniyor…", en: "Retrying…" })}
      retry={() => query.refetch()}
      retrying={query.isFetching}
    /></div> : latest ? <div className="mt-3 rounded-lg border bg-muted/20 p-3 text-sm">
      <p className="font-medium">{latest.completed_course_count} {pick({ tr: "tamamlanmış ders", en: "completed courses" })} · {latest.enrolled_course_count ? `${latest.enrolled_course_count} ${pick({ tr: "kayıtlı ders", en: "enrolled courses" })}` : pick({ tr: "Bu dönem kayıtlı ders yok", en: "No courses enrolled this term" })}</p>
      <p className="mt-1 text-xs text-muted-foreground">{pick({ tr: "Dönem", en: "Term" })}: {formatAcademicTerm(latest.term, locale)}</p>
      <p className="mt-0.5 text-xs text-muted-foreground">{pick({ tr: "Son yenileme", en: "Last refreshed" })}: {new Date(latest.fetched_at).toLocaleDateString(locale === "tr" ? "tr-TR" : "en-US", { dateStyle: "medium" })} · {new Date(latest.fetched_at).toLocaleTimeString(locale === "tr" ? "tr-TR" : "en-US", { hour: "2-digit", minute: "2-digit" })}</p>
      {latest.completed_course_codes.length ? <>
        <p className={`mt-2 text-xs leading-5 text-muted-foreground ${coursesExpanded ? "" : "line-clamp-2"}`}>{latest.completed_course_codes.join(" · ")}</p>
        {latest.completed_course_codes.length > 8 ? <Button variant="ghost" size="sm" className="mt-1 h-8 px-2 text-xs" aria-expanded={coursesExpanded} onClick={() => setCoursesExpanded((current) => !current)}>{coursesExpanded ? pick({ tr: "Dersleri daralt", en: "Show fewer courses" }) : pick({ tr: `Tüm ${latest.completed_course_count} dersi göster`, en: `Show all ${latest.completed_course_count} courses` })}</Button> : null}
      </> : null}
    </div> : <p className="mt-3 rounded-lg border border-dashed bg-muted/15 p-3 text-sm text-muted-foreground">{pick({ tr: "Henüz kayıtlı akademik veri yok. Bir dönem seçip ODTÜ'den yenileyebilirsin.", en: "No academic data is stored yet. Choose a term and refresh it from METU." })}</p>}
    <div className="mt-4 flex flex-wrap items-end gap-2"><div className="space-y-1"><Label htmlFor="academic-term" className="text-xs">{pick({ tr: "Dönem", en: "Term" })}</Label><select id="academic-term" value={term} onChange={(event) => setTerm(event.target.value)} className="h-10 rounded-md border bg-background px-3 text-sm">{AVAILABLE_TERMS.map((item) => <option key={item.value} value={item.value}>{item[locale]}</option>)}</select></div><Button variant="outline" disabled={sync.isPending || term.trim().length < 3} onClick={() => sync.mutate()}>{sync.isPending ? <Loader2Icon className="animate-spin" /> : <RotateCcwIcon />}{pick({ tr: "ODTÜ'den yenile", en: "Refresh from METU" })}</Button>
      {/* Kept from the planner work: the read really does take a minute or two,
          and a button that looks stuck is the reason people reload mid-scrape. */}
      {sync.isPending ? <p className="w-full text-xs text-muted-foreground">{pick({ tr: "ODTÜ kayıtların okunuyor; bu bir iki dakika sürebilir. Sayfayı açık bırak.", en: "Your METU records are being read; this can take a minute or two. Keep the page open." })}</p> : null}
      {query.data?.has_cached_data ? <AlertDialog><AlertDialogTrigger render={<Button variant="outline" className="text-destructive" />}><Trash2Icon />{pick({ tr: "Akademik verileri sıfırla", en: "Reset academic data" })}</AlertDialogTrigger><AlertDialogContent><AlertDialogHeader><AlertDialogTitle>{pick({ tr: "Akademik veriler silinsin mi?", en: "Delete academic data?" })}</AlertDialogTitle><AlertDialogDescription>{pick({ tr: "Kayıtlı bölüm ve transkript dersleri kalıcı olarak silinir. ODTÜ bağlantın ve giriş bilgilerin silinmez; gerektiğinde veriler tekrar alınabilir.", en: "Stored department and transcript courses are permanently deleted. Your METU connection and credentials remain; the data can be fetched again when needed." })}</AlertDialogDescription></AlertDialogHeader><AlertDialogFooter><AlertDialogCancel>{pick({ tr: "Vazgeç", en: "Cancel" })}</AlertDialogCancel><AlertDialogAction variant="destructive" disabled={reset.isPending} onClick={() => reset.mutate()}>{reset.isPending ? <Loader2Icon className="animate-spin" /> : <Trash2Icon />}{pick({ tr: "Sil ve sıfırla", en: "Delete and reset" })}</AlertDialogAction></AlertDialogFooter></AlertDialogContent></AlertDialog> : null}
    </div>
  </div>;
}

function PreferenceEditor() {
  const { pick } = useLocale();
  const client = useQueryClient();
  const [interests, setInterests] = useState("");
  const [device, setDevice] = useState("unspecified");
  const deviceOptions = [
    { value: "unspecified", label: pick({ tr: "Belirtilmedi", en: "Not specified" }) },
    { value: "ios", label: "iPhone / iPad" },
    { value: "android", label: "Android" },
    { value: "windows", label: "Windows" },
    { value: "macos", label: "macOS" },
    { value: "linux", label: "Linux" },
  ];
  const query = useQuery({ queryKey: ["student", "preferences"], queryFn: () => jsonFetch<{ items: PreferenceItem[] }>("/api/student/preferences") });
  const save = useMutation({ mutationFn: ({ key, value }: { key: string; value: Record<string, unknown> }) => jsonFetch<void>(`/api/student/preferences/${key}`, { method: "PUT", body: { value } }), onSuccess: () => { toast.success(pick({ tr: "Tercih kaydedildi", en: "Preference saved" })); void client.invalidateQueries({ queryKey: ["student", "preferences"] }); }, onError: (error) => toast.error(error.message) });
  const remove = useMutation({ mutationFn: (key: string) => jsonFetch<void>(`/api/student/preferences/${key}`, { method: "DELETE" }), onSuccess: () => void client.invalidateQueries({ queryKey: ["student", "preferences"] }) });
  const preferenceLabel = (key: string) => ({ interests: pick({ tr: "İlgi alanları", en: "Interests" }), device_platform: pick({ tr: "Cihaz", en: "Device" }) } as Record<string, string>)[key] ?? key.replaceAll("_", " ");
  const provenanceLabel = (value: PreferenceItem["provenance"]) => value === "explicit" ? pick({ tr: "sen ekledin", en: "added by you" }) : pick({ tr: "öğrenildi", en: "learned" });

  return <section aria-labelledby="preference-heading" className="space-y-4">
    <div>
      <p id="preference-heading" className="font-semibold">{pick({ tr: "Kişisel tercihler", en: "Personal preferences" })}</p>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">{pick({ tr: "İlgi alanların önerileri, cihazın ise dosya ve kullanım yönergelerini sana uygun hale getirir.", en: "Your interests tailor recommendations; your device tailors file and usage instructions." })}</p>
    </div>
    <div className="grid gap-3 sm:grid-cols-[1fr_12rem_auto]">
      <div className="space-y-2"><Label htmlFor="interests">{pick({ tr: "İlgi alanları", en: "Interests" })}</Label><Input id="interests" value={interests} onChange={(event) => setInterests(event.target.value)} placeholder={pick({ tr: "sinema, caz, robotik", en: "cinema, jazz, robotics" })} /></div>
      <div className="space-y-2"><Label htmlFor="device-platform">{pick({ tr: "Cihaz", en: "Device" })}</Label><Select value={device} onValueChange={(value) => setDevice(value ?? "unspecified")}><SelectTrigger id="device-platform" aria-label={pick({ tr: "Cihaz", en: "Device" })} className="w-full"><SelectValue>{deviceOptions.find((option) => option.value === device)?.label}</SelectValue></SelectTrigger><SelectContent>{deviceOptions.map((option) => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent></Select></div>
      <div className="flex items-end"><Button variant="outline" className="disabled:bg-muted disabled:text-muted-foreground disabled:opacity-100" disabled={save.isPending || (!interests.trim() && device === "unspecified")} onClick={() => { if (interests.trim()) save.mutate({ key: "interests", value: { items: interests.split(",").map((item) => item.trim()).filter(Boolean) } }); if (device !== "unspecified") save.mutate({ key: "device_platform", value: { platform: device } }); }}>{pick({ tr: "Tercihleri kaydet", en: "Save preferences" })}</Button></div>
    </div>
    {query.isLoading ? <div className="flex min-h-12 items-center gap-2 text-sm text-muted-foreground"><Loader2Icon className="size-4 animate-spin" />{pick({ tr: "Tercihler yükleniyor…", en: "Loading preferences…" })}</div> : query.error ? <SettingsQueryError
      message={pick({ tr: "Tercihler yüklenemedi.", en: "Preferences could not be loaded." })}
      retryLabel={pick({ tr: "Tekrar dene", en: "Try again" })}
      retryingLabel={pick({ tr: "Yenileniyor…", en: "Retrying…" })}
      retry={() => query.refetch()}
      retrying={query.isFetching}
    /> : query.data?.items.length ? <div className="flex flex-wrap gap-2">{query.data.items.map((item) => <Badge key={item.key} variant="secondary" className="gap-2 py-1.5">{preferenceLabel(item.key)} · {provenanceLabel(item.provenance)}<button type="button" className="rounded-full hover:text-destructive" aria-label={pick({ tr: `${preferenceLabel(item.key)} tercihini sil`, en: `Delete ${preferenceLabel(item.key)} preference` })} onClick={() => remove.mutate(item.key)}>×</button></Badge>)}</div> : <p className="rounded-lg border border-dashed bg-muted/15 p-3 text-sm text-muted-foreground">{pick({ tr: "Henüz kaydedilmiş bir tercih yok.", en: "No saved preferences yet." })}</p>}
  </section>;
}
