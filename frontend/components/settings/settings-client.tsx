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
import { cn } from "@/lib/utils";
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

export function SettingsClient() {
  const { pick } = useLocale();
  const { profile, update: updateProfile } = useProfile();
  const { memories, isLoading: memoriesLoading, clear: clearMemories } = useMemories();

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
    { href: "#memory", icon: BrainIcon, label: pick({ tr: "Hatırlananlar", en: "Remembered items" }) },
    { href: "#personalization", icon: SparklesIcon, label: pick({ tr: "Kişiselleştirme", en: "Personalization" }) },
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

      <div className="mt-6 grid items-start gap-6 lg:grid-cols-[14rem_minmax(0,1fr)]">
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

        <main className="min-w-0 space-y-5">
          <CampusConnectionCard />

          <Card id="personalization" className="motion-enter surface-raised scroll-mt-24 border-0 ring-1 ring-foreground/8 [animation-delay:40ms]">
            <CardHeader className="border-b bg-muted/20"><div className="flex items-start gap-3"><span className="grid size-9 shrink-0 place-items-center rounded-xl bg-primary/10 text-primary"><SparklesIcon className="size-4" /></span><div><CardTitle>{pick({ tr: "Kişiselleştirme ve güncellemeler", en: "Personalization and updates" })}</CardTitle><CardDescription className="mt-1">{pick({ tr: "İlgi alanlarını düzenle ve e-postadan yalnızca yapılandırılmış tarih/etkinlik bilgisi çıkarılmasına izin ver.", en: "Edit your interests and choose whether email may yield structured date and event facts." })}</CardDescription></div></div></CardHeader>
            <CardContent className="space-y-5"><AcademicContextEditor /><AcademicDataManager /><PreferenceEditor /><div className="grid grid-cols-[1fr_auto] items-start gap-4 rounded-xl border bg-background/55 p-4"><div><Label htmlFor="mail-facts" className="font-semibold">{pick({ tr: "E-postadan yapılandırılmış güncellemeler", en: "Structured updates from email" })}</Label><p className="mt-1 text-sm leading-5 text-muted-foreground">{pick({ tr: "Yalnızca etkinlik veya son tarih başlığı, özeti ve zamanı saklanır. Ham e-posta gövdesi kaydedilmez ve vektör dizinine gönderilmez.", en: "Only an event or deadline title, summary, and time are kept. Raw email bodies are not stored or embedded." })}</p></div><Switch id="mail-facts" checked={profile?.mail_facts_enabled ?? false} disabled={!profile || updateProfile.isPending} onCheckedChange={(checked) => void updateProfile.mutateAsync({ mail_facts_enabled: checked }).catch((error) => toast.error(error instanceof Error ? error.message : "Update failed"))} /></div></CardContent>
          </Card>

          <Card id="memory" className="motion-enter surface-raised scroll-mt-24 border-0 ring-1 ring-foreground/8 [animation-delay:70ms]">
            <CardHeader className="border-b bg-muted/20">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="flex items-start gap-3">
                  <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-primary/10 text-primary"><BrainIcon className="size-4" /></span>
                  <div><CardTitle>{pick({ tr: "Hatırlanan tercihler", en: "Remembered preferences" })}</CardTitle><CardDescription className="mt-1">{pick({ tr: "Yalnızca açıkça hatırlamasını istediğin, hassas olmayan tercihler burada tutulur.", en: "Only non-sensitive preferences you explicitly asked the assistant to remember are kept here." })}</CardDescription></div>
                </div>
                {!memoriesLoading ? <Badge variant="secondary">{memories.length} {pick({ tr: "kayıt", en: memories.length === 1 ? "item" : "items" })}</Badge> : null}
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              {memoriesLoading ? (
                <div className="flex min-h-28 items-center justify-center gap-2 text-sm text-muted-foreground"><Loader2Icon className="size-4 animate-spin" />{pick({ tr: "Hatırlananlar yükleniyor…", en: "Loading remembered items…" })}</div>
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

          <Card id="setup" className="motion-enter surface-raised scroll-mt-24 border-0 ring-1 ring-foreground/8 [animation-delay:110ms]">
            <CardHeader className="border-b bg-muted/20">
              <div className="flex items-start gap-3"><span className="grid size-9 shrink-0 place-items-center rounded-xl bg-muted text-muted-foreground"><RotateCcwIcon className="size-4" /></span><div><CardTitle>{pick({ tr: "Kurulumu yeniden gözden geçir", en: "Review setup again" })}</CardTitle><CardDescription className="mt-1">{pick({ tr: "Dil, hitap şekli, ODTÜ bağlantısı ve veri erişimi seçimlerini adım adım yeniden incele. Mevcut kayıtların silinmez.", en: "Review language, how you're addressed, your METU connection, and data-access choices step by step. Existing data is not deleted." })}</CardDescription></div></div>
            </CardHeader>
            <CardContent>
              <Button variant="outline" disabled={updateProfile.isPending} onClick={() => void reopenSetup()}>{updateProfile.isPending ? <Loader2Icon className="animate-spin" /> : <RotateCcwIcon />}{pick({ tr: "Kurulum adımlarını aç", en: "Open setup steps" })}</Button>
            </CardContent>
          </Card>
        </main>
      </div>
    </div>
  );
}

type PreferenceItem = { key: string; value: Record<string, unknown>; provenance: "explicit" | "learned"; confidence: number; updated_at: string };

type AcademicContext = { department: string | null; surname_prefix: string | null; degree_level: string | null; year_of_study: number | null; program_code: string | null; campus: string | null; source: string; verified_at: string | null; confirmed_at: string | null };

function AcademicContextEditor() {
  const { pick } = useLocale(); const client = useQueryClient();
  const query = useQuery({ queryKey: ["student", "context"], queryFn: () => jsonFetch<AcademicContext>("/api/student/context") });
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Partial<AcademicContext>>({});
  const mutation = useMutation({
    mutationFn: (body: Record<string, unknown>) => jsonFetch<AcademicContext>("/api/student/context", { method: "PUT", body }),
    onSuccess: (data) => { client.setQueryData(["student", "context"], data); setEditing(false); setDraft({}); toast.success(pick({ tr: "Akademik bağlam kaydedildi", en: "Academic context saved" })); },
    onError: (error) => toast.error(error.message),
  });
  const context = query.data;
  const value = <K extends keyof AcademicContext>(key: K) => (draft[key] ?? context?.[key] ?? "") as string;

  // Saving sends the draft alone — only the fields actually typed into. It used
  // to send every rendered value, so a save from a form that had not finished
  // loading overwrote a department SAIS had just read with an empty string.
  // The server patches what it is given and leaves everything else untouched.

  // What the planner and chat actually need before they can answer anything.
  const rows: Array<{ key: keyof AcademicContext; label: string; hint: string }> = [
    { key: "department", label: pick({ tr: "Bölüm", en: "Department" }), hint: pick({ tr: "Hangi derslerin açık olduğunu belirler", en: "Decides which courses are open to you" }) },
    { key: "surname_prefix", label: pick({ tr: "Soyadı (ilk 2 harf)", en: "Surname (first 2 letters)" }), hint: pick({ tr: "Şube soyadı kısıtları için", en: "For section surname restrictions" }) },
    { key: "year_of_study", label: pick({ tr: "Sınıf", en: "Year of study" }), hint: pick({ tr: "Bazı şubeler yalnızca belirli sınıflara açık", en: "Some sections are open to one year only" }) },
    { key: "program_code", label: pick({ tr: "Program kodu", en: "Program code" }), hint: pick({ tr: "İsteğe bağlı", en: "Optional" }) },
    { key: "campus", label: pick({ tr: "Kampüs", en: "Campus" }), hint: pick({ tr: "İsteğe bağlı", en: "Optional" }) },
  ];
  const optional: Array<keyof AcademicContext> = ["program_code", "campus", "year_of_study"];
  const missing = rows.filter((row) => !optional.includes(row.key) && !value(row.key));

  return <div className="rounded-xl border bg-background/55 p-4">
    <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
      <div>
        <p className="font-semibold">{pick({ tr: "Akademik bağlam", en: "Academic context" })}</p>
        <p className="mt-0.5 max-w-xl text-xs leading-5 text-muted-foreground">{pick({ tr: "Ders planlayıcı ve sohbet bu bilgileri kullanır. Normalde ODTÜ kayıtlarından bir kez okunur; okunamazsa elle girebilirsin.", en: "The planner and chat use these. They are read from your METU record once; you can fill them in by hand if that fails." })}</p>
      </div>
      {context ? <Badge variant={context.verified_at ? "secondary" : "outline"}>{context.source === "sais" ? "SAIS" : pick({ tr: "elle", en: "manual" })}</Badge> : null}
    </div>

    {missing.length && !editing ? <p className="mb-3 rounded-lg border border-amber-500/30 bg-amber-500/5 p-2 text-xs leading-5">
      {pick({ tr: `${missing.map((row) => row.label).join(" ve ")} eksik. SAIS'ten okunamadıysa aşağıdan elle girebilirsin.`, en: `${missing.map((row) => row.label).join(" and ")} missing. If your METU record did not provide it, fill it in below.` })}
    </p> : null}

    {editing ? <div className="grid gap-3 sm:grid-cols-2">
      {rows.map((row) => <div key={row.key} className="space-y-1">
        <Label className="text-xs">{row.label}</Label>
        <Input value={value(row.key)} maxLength={row.key === "surname_prefix" ? 2 : undefined} onChange={(event) => setDraft((current) => ({ ...current, [row.key]: event.target.value }))} />
        <p className="text-[11px] text-muted-foreground">{row.hint}</p>
      </div>)}
      <div className="flex items-end gap-2 sm:col-span-2">
        <Button disabled={mutation.isPending} onClick={() => mutation.mutate({ ...draft, ...(draft.year_of_study === undefined ? {} : { year_of_study: Number(draft.year_of_study) || null }) })}>{mutation.isPending ? <Loader2Icon className="animate-spin" /> : null}{pick({ tr: "Kaydet", en: "Save" })}</Button>
        <Button variant="ghost" onClick={() => { setEditing(false); setDraft({}); }}>{pick({ tr: "Vazgeç", en: "Cancel" })}</Button>
      </div>
    </div> : <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
      {rows.map((row) => <div key={row.key}>
        <p className="text-[11px] uppercase tracking-wide text-muted-foreground">{row.label}</p>
        <p className={cn("text-sm", value(row.key) ? "font-medium" : "text-muted-foreground")}>{value(row.key) || pick({ tr: "—", en: "—" })}</p>
      </div>)}
      <Button variant="outline" size="sm" className="ml-auto" onClick={() => setEditing(true)}>{pick({ tr: "Düzenle", en: "Edit" })}</Button>
    </div>}
  </div>;
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
      {query.isLoading ? <Loader2Icon className="size-4 animate-spin text-muted-foreground" /> : <Badge variant={query.data?.has_cached_data ? "secondary" : "outline"}>{query.data?.has_cached_data ? pick({ tr: "Kayıtlı", en: "Stored" }) : pick({ tr: "Henüz alınmadı", en: "Not fetched" })}</Badge>}
    </div>
    {latest ? <div className="mt-3 rounded-lg border bg-muted/20 p-3 text-sm"><p className="font-medium">{latest.completed_course_count} {pick({ tr: "tamamlanmış ders", en: "completed courses" })} · {latest.enrolled_course_count} {pick({ tr: "kayıtlı ders", en: "enrolled courses" })}</p><p className="mt-1 text-xs text-muted-foreground">{pick({ tr: "Dönem", en: "Term" })}: {formatAcademicTerm(latest.term, locale)} · {new Date(latest.fetched_at).toLocaleDateString(locale === "tr" ? "tr-TR" : "en-US")}</p>{latest.completed_course_codes.length ? <p className="mt-2 line-clamp-2 text-xs text-muted-foreground">{latest.completed_course_codes.join(" · ")}</p> : null}</div> : null}
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
  const query = useQuery({ queryKey: ["student", "preferences"], queryFn: () => jsonFetch<{ items: PreferenceItem[] }>("/api/student/preferences") });
  const save = useMutation({ mutationFn: ({ key, value }: { key: string; value: Record<string, unknown> }) => jsonFetch<void>(`/api/student/preferences/${key}`, { method: "PUT", body: { value } }), onSuccess: () => { toast.success(pick({ tr: "Tercih kaydedildi", en: "Preference saved" })); void client.invalidateQueries({ queryKey: ["student", "preferences"] }); }, onError: (error) => toast.error(error.message) });
  const remove = useMutation({ mutationFn: (key: string) => jsonFetch<void>(`/api/student/preferences/${key}`, { method: "DELETE" }), onSuccess: () => void client.invalidateQueries({ queryKey: ["student", "preferences"] }) });
  return <div className="space-y-4"><div className="grid gap-3 sm:grid-cols-[1fr_12rem_auto]"><div className="space-y-2"><Label htmlFor="interests">{pick({ tr: "İlgi alanları", en: "Interests" })}</Label><Input id="interests" value={interests} onChange={(event) => setInterests(event.target.value)} placeholder={pick({ tr: "sinema, caz, robotik", en: "cinema, jazz, robotics" })} /></div><div className="space-y-2"><Label>{pick({ tr: "Cihaz", en: "Device" })}</Label><Select value={device} onValueChange={(value) => setDevice(value ?? "unspecified")}><SelectTrigger><SelectValue /></SelectTrigger><SelectContent>{["unspecified", "ios", "android", "windows", "macos", "linux"].map((value) => <SelectItem key={value} value={value}>{value}</SelectItem>)}</SelectContent></Select></div><div className="flex items-end"><Button variant="outline" disabled={save.isPending || (!interests.trim() && device === "unspecified")} onClick={() => { if (interests.trim()) save.mutate({ key: "interests", value: { items: interests.split(",").map((item) => item.trim()).filter(Boolean) } }); if (device !== "unspecified") save.mutate({ key: "device_platform", value: { platform: device } }); }}>{pick({ tr: "Kaydet", en: "Save" })}</Button></div></div>{query.data?.items.length ? <div className="flex flex-wrap gap-2">{query.data.items.map((item) => <Badge key={item.key} variant="secondary" className="gap-2 py-1.5">{item.key.replaceAll("_", " ")} · {item.provenance}<button type="button" className="rounded-full hover:text-destructive" aria-label={pick({ tr: "Tercihi sil", en: "Delete preference" })} onClick={() => remove.mutate(item.key)}>×</button></Badge>)}</div> : null}</div>;
}
