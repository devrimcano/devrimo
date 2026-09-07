"use client";

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLinkIcon, GraduationCapIcon, PlayIcon, RefreshCwIcon, RotateCcwIcon } from "lucide-react";
import { toast } from "sonner";
import { captureProductEvent } from "@/components/posthog-analytics";
import { useLocale } from "@/components/locale-provider";
import { EmptyState, ErrorState, PanelHeader, SearchField, formatDate } from "@/components/admin/admin-shared";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { adminGet, adminMutate } from "@/lib/admin/client";
import type { AdminPrincipal } from "@/lib/admin/types";
import type { components } from "@/lib/api/schema";

type Dashboard = components["schemas"]["DashboardView"];
type Run = components["schemas"]["RunView"];
type Items = components["schemas"]["ItemPage"];
type Directory = components["schemas"]["ResearcherPage"];

function ImportStatus({ status }: { status: string }) {
  const { pick } = useLocale();
  const labels: Record<string, { tr: string; en: string }> = {
    queued: { tr: "Sırada", en: "Queued" }, running: { tr: "İçe aktarılıyor", en: "Importing" },
    completed: { tr: "Tamamlandı", en: "Completed" }, incomplete: { tr: "Eksikler var", en: "Incomplete" },
    interrupted: { tr: "Kesintiye uğradı", en: "Interrupted" }, pending: { tr: "Bekliyor", en: "Pending" },
  };
  return <Badge variant={status === "incomplete" || status === "interrupted" ? "destructive" : "secondary"}>{labels[status] ? pick(labels[status]) : status}</Badge>;
}

function Pages({ offset, total, onChange }: { offset: number; total: number; onChange: (offset: number) => void }) {
  const { pick } = useLocale();
  return <div className="flex items-center justify-between gap-3 border-t px-4 py-3 text-xs text-muted-foreground">
    <span>{total ? `${offset + 1}–${Math.min(offset + 25, total)} / ${total}` : "0"}</span>
    <div className="flex gap-2"><Button size="sm" variant="outline" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - 25))}>{pick({ tr: "Önceki", en: "Previous" })}</Button><Button size="sm" variant="outline" disabled={offset + 25 >= total} onClick={() => onChange(offset + 25)}>{pick({ tr: "Sonraki", en: "Next" })}</Button></div>
  </div>;
}

export function ResearchersPanel({ principal, title, description }: { principal: AdminPrincipal; title: string; description: string }) {
  const { pick, locale } = useLocale();
  const cache = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [itemOffset, setItemOffset] = useState(0);
  const [search, setSearch] = useState("");
  const [queryText, setQueryText] = useState("");
  const [directoryOffset, setDirectoryOffset] = useState(0);
  const [startOpen, setStartOpen] = useState(false);
  const [sample, setSample] = useState("all");
  const viewTracked = useRef(false);
  useEffect(() => {
    if (viewTracked.current) return;
    viewTracked.current = true;
    captureProductEvent("admin_section_viewed", { section: "researchers" });
  }, []);
  const writable = principal.permissions.includes("researchers:write");
  useEffect(() => { const timer = setTimeout(() => setQueryText(search.trim()), 300); return () => clearTimeout(timer); }, [search]);
  const dashboard = useQuery({ queryKey: ["admin", "researchers", "dashboard"], queryFn: () => adminGet<Dashboard>("researchers/dashboard"), refetchInterval: 5000 });
  const data = dashboard.data;
  const selected = data?.runs.find((run) => run.id === selectedId) ?? data?.runs[0];
  const blocked = !data || data.busy || data.runs.some((run) => run.status === "queued");
  const items = useQuery({ queryKey: ["admin", "researchers", "items", selected?.id, itemOffset, errorsOnly], enabled: !!selected,
    queryFn: () => adminGet<Items>(`researchers/runs/${selected!.id}/items?offset=${itemOffset}&errors_only=${errorsOnly}`), refetchInterval: 5000 });
  const directory = useQuery({ queryKey: ["admin", "researchers", "directory", queryText, directoryOffset],
    queryFn: () => adminGet<Directory>(`researchers?q=${encodeURIComponent(queryText)}&offset=${directoryOffset}`), refetchInterval: 10000 });
  const action = useMutation({ mutationFn: ({ resumeId }: { resumeId?: string }) => resumeId
    ? adminMutate<Run>(`researchers/runs/${resumeId}/resume`, "POST", {})
    : adminMutate<Run>("researchers/runs", "POST", { limit: sample === "all" ? null : Number(sample) }),
    onSuccess: (run) => { setSelectedId(run.id); setItemOffset(0); setStartOpen(false); toast.success(pick({ tr: "İçe aktarma sıraya alındı", en: "Import queued" })); void cache.invalidateQueries({ queryKey: ["admin", "researchers"] }); },
    onError: (error) => { toast.error(error.message); void dashboard.refetch(); },
  });
  const refresh = () => void cache.invalidateQueries({ queryKey: ["admin", "researchers"] });

  return <>
    <PanelHeader title={title} description={description} actions={<div className="flex gap-2"><Button size="sm" variant="outline" onClick={refresh} disabled={dashboard.isFetching}><RefreshCwIcon />{pick({ tr: "Yenile", en: "Refresh" })}</Button>{writable ? <Button size="sm" disabled={blocked || action.isPending} onClick={() => setStartOpen(true)}><PlayIcon />{pick({ tr: "Yeni içe aktarma", en: "New import" })}</Button> : null}</div>} />
    {dashboard.isLoading ? <Skeleton className="h-48 rounded-xl" /> : dashboard.error ? <ErrorState error={dashboard.error} retry={() => void dashboard.refetch()} /> : data ? <div className="space-y-5">
      <div className="grid gap-3 sm:grid-cols-3">
        {[{ label: pick({ tr: "Kayıtlı araştırmacı", en: "Saved researchers" }), value: data.researchers.toLocaleString(locale) },
          { label: pick({ tr: "Kayıtlı bölüm ve çalışma", en: "Saved sections and works" }), value: data.sections.toLocaleString(locale) },
          { label: pick({ tr: "Bağlantı", en: "Connection" }), value: data.proxy_enabled ? pick({ tr: "Proxy yapılandırıldı", en: "Proxy configured" }) : pick({ tr: "Doğrudan", en: "Direct" }) }].map((stat) => <Card key={stat.label}><CardContent><p className="text-xs text-muted-foreground">{stat.label}</p><p className="mt-2 text-2xl font-semibold tabular-nums">{stat.value}</p></CardContent></Card>)}
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"><Badge variant="outline">AVESIS · English</Badge><span>{pick({ tr: "Her seferinde bir istek. Veriler toplandıkça kaydedilir; devam etme işlemi tamamlanan kayıtları atlar.", en: "One request at a time. Data is saved as it arrives; resuming skips completed work." })}</span></div>
      {selected ? <Card>
        <CardHeader className="flex flex-wrap items-start justify-between gap-3 sm:flex-row"><div><CardTitle>{pick({ tr: "İçe aktarma ilerlemesi", en: "Import progress" })}</CardTitle><p className="mt-1 text-xs text-muted-foreground">{formatDate(selected.started_at, locale)} · {selected.limit ? pick({ tr: `${selected.limit} kişilik örnek`, en: `${selected.limit}-researcher sample` }) : pick({ tr: "Tüm araştırmacılar", en: "All researchers" })}</p></div><div className="flex gap-2"><ImportStatus status={selected.status} />{writable && ["incomplete", "interrupted"].includes(selected.status) ? <Button size="sm" variant="outline" disabled={blocked || action.isPending} onClick={() => action.mutate({ resumeId: selected.id })}><RotateCcwIcon />{pick({ tr: "Devam et", en: "Resume" })}</Button> : null}</div></CardHeader>
        <CardContent className="space-y-4">
          <div className="flex justify-between text-sm"><span>{selected.selected ? pick({ tr: `${selected.completed} / ${selected.selected} araştırmacı tamamlandı`, en: `${selected.completed} / ${selected.selected} researchers complete` }) : selected.status === "queued" ? pick({ tr: "Başlaması bekleniyor", en: "Waiting to start" }) : selected.status === "running" ? pick({ tr: "Araştırmacı listesi bulunuyor…", en: "Discovering researcher profiles…" }) : pick({ tr: "Araştırmacı listesi henüz alınamadı", en: "Researcher discovery has not completed" })}</span><span className="tabular-nums">{selected.selected ? `${Math.floor(selected.completed / selected.selected * 100)}%` : "—"}</span></div>
          <Progress value={selected.selected ? selected.completed / selected.selected * 100 : 0} aria-label={pick({ tr: "Tamamlanan araştırmacılar", en: "Completed researchers" })} />
          <div className="flex flex-wrap gap-4 text-xs text-muted-foreground"><span>{pick({ tr: "Bekleyen", en: "Pending" })}: {selected.pending}</span><span>{pick({ tr: "Eksik", en: "Incomplete" })}: {selected.incomplete}</span><span>{pick({ tr: "Şu an işlenen", en: "In progress" })}: {selected.status === "running" ? selected.running : 0}</span></div>
          {selected.status === "interrupted" ? <p className="rounded-lg bg-muted p-3 text-sm">{pick({ tr: "İçe aktarma işlemi artık çalışmıyor. Kaydedilen ilerleme korunuyor; Devam et ile kaldığı yerden sürdürülebilir.", en: "The importer is no longer running. Saved progress is intact; Resume continues unfinished work." })}</p> : null}
          {selected.last_error ? <p role="alert" className="break-words text-sm text-destructive">{selected.last_error}</p> : null}
          <p className="text-xs text-muted-foreground">{pick({ tr: "Tamamlanma, desteklenen sayfa bölümlerini kapsar. Etkileşimli ek metrik panelleri bağlantı olarak saklanır.", en: "Completion covers supported page sections. Additional interactive metrics panels are retained as links." })}</p>
        </CardContent>
      </Card> : <EmptyState icon={<GraduationCapIcon className="size-5" />} title={pick({ tr: "Henüz içe aktarma yok", en: "No imports yet" })} description={pick({ tr: "AVESIS araştırmacılarını toplamak için yeni bir içe aktarma başlatın.", en: "Start an import to collect AVESIS researcher profiles." })} />}
      {data.runs.length ? <Card><CardHeader><CardTitle>{pick({ tr: "Son içe aktarmalar", en: "Recent imports" })}</CardTitle></CardHeader><CardContent className="space-y-2">{data.runs.map((run) => <button key={run.id} aria-pressed={selected?.id === run.id} className={`flex w-full flex-wrap items-center justify-between gap-3 rounded-lg border p-3 text-left text-sm transition-colors hover:bg-muted ${selected?.id === run.id ? "border-primary/40 bg-primary/5" : "border-border"}`} onClick={() => { setSelectedId(run.id); setItemOffset(0); }}><span>{formatDate(run.started_at, locale)}<span className="ml-2 text-xs text-muted-foreground">{run.completed}/{run.selected || "—"}</span></span><ImportStatus status={run.status} /></button>)}</CardContent></Card> : null}
      {selected ? <Card className="overflow-hidden"><CardHeader className="flex flex-wrap items-center justify-between gap-3 sm:flex-row"><CardTitle>{pick({ tr: "İçe aktarma ayrıntıları", en: "Import details" })}</CardTitle><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={errorsOnly} onChange={(event) => { setErrorsOnly(event.target.checked); setItemOffset(0); }} />{pick({ tr: "Yalnızca eksikler", en: "Incomplete only" })}</label></CardHeader><CardContent className="px-0">
        {items.isLoading ? <Skeleton className="mx-4 h-32" /> : items.error ? <ErrorState error={items.error} retry={() => void items.refetch()} /> : <><Table><TableHeader><TableRow><TableHead className="pl-4">{pick({ tr: "Araştırmacı", en: "Researcher" })}</TableHead><TableHead>{pick({ tr: "Durum", en: "Status" })}</TableHead><TableHead>{pick({ tr: "Kaydedilen bölümler", en: "Saved sections" })}</TableHead><TableHead>{pick({ tr: "Sorun", en: "Issue" })}</TableHead></TableRow></TableHeader><TableBody>{items.data?.items.map((item) => <TableRow key={item.id}><TableCell className="min-w-48 pl-4"><a href={item.source_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 font-medium hover:underline">{item.name}<ExternalLinkIcon className="size-3 shrink-0" /></a></TableCell><TableCell><ImportStatus status={item.status === "running" && selected.status !== "running" ? "interrupted" : item.status} /></TableCell><TableCell>{item.completed_sections}</TableCell><TableCell className="max-w-96 whitespace-normal text-xs text-destructive">{item.errors.map((error, index) => <p key={index}>{error.section}: {error.error}</p>)}</TableCell></TableRow>)}</TableBody></Table>{!items.data?.items.length ? <p className="p-5 text-sm text-muted-foreground">{pick({ tr: "Bu görünümde kayıt yok.", en: "No records in this view." })}</p> : null}<Pages offset={itemOffset} total={items.data?.total ?? 0} onChange={setItemOffset} /></>}
      </CardContent></Card> : null}
      <Card className="overflow-hidden"><CardHeader><CardTitle>{pick({ tr: "Kayıtlı araştırmacılar", en: "Saved researchers" })}</CardTitle><SearchField value={search} onChange={(value) => { setSearch(value); setDirectoryOffset(0); }} placeholder={pick({ tr: "Ad veya bölüm ara", en: "Search name or department" })} /></CardHeader><CardContent className="px-0">{directory.isLoading ? <Skeleton className="mx-4 h-32" /> : directory.error ? <ErrorState error={directory.error} retry={() => void directory.refetch()} /> : <><Table><TableHeader><TableRow><TableHead className="pl-4">{pick({ tr: "Araştırmacı", en: "Researcher" })}</TableHead><TableHead>{pick({ tr: "Birim", en: "Affiliation" })}</TableHead><TableHead>{pick({ tr: "Son okuma", en: "Last read" })}</TableHead></TableRow></TableHeader><TableBody>{directory.data?.items.map((person) => <TableRow key={person.id}><TableCell className="min-w-48 pl-4"><a href={person.source_url} target="_blank" rel="noopener noreferrer" className="font-medium hover:underline">{person.title} {person.name}</a></TableCell><TableCell className="max-w-96 whitespace-normal text-xs text-muted-foreground">{person.affiliation || "—"}</TableCell><TableCell className="text-xs text-muted-foreground">{formatDate(person.last_seen_at, locale)}</TableCell></TableRow>)}</TableBody></Table>{!directory.data?.items.length ? <p className="p-5 text-sm text-muted-foreground">{pick({ tr: "Araştırmacı bulunamadı.", en: "No researchers found." })}</p> : null}<Pages offset={directoryOffset} total={directory.data?.total ?? 0} onChange={setDirectoryOffset} /></>}</CardContent></Card>
    </div> : null}
    <Dialog open={startOpen} onOpenChange={setStartOpen}><DialogContent><DialogHeader><DialogTitle>{pick({ tr: "Yeni içe aktarma", en: "Start a new import" })}</DialogTitle><DialogDescription>{pick({ tr: "İngilizce AVESIS profilleri toplanır. Mevcut kayıtlar çoğaltılmadan güncellenir. Kesilmiş bir işlemi sürdürmek için bunun yerine Devam et seçeneğini kullanın.", en: "Collect English AVESIS profiles. Existing records are updated without duplicates. To continue an interrupted run, use Resume instead." })}</DialogDescription></DialogHeader><div className="space-y-2"><Label htmlFor="researcher-import-size">{pick({ tr: "İçe aktarma kapsamı", en: "Import size" })}</Label><select id="researcher-import-size" value={sample} onChange={(event) => setSample(event.target.value)} className="h-10 w-full rounded-md border bg-background px-3 text-sm"><option value="all">{pick({ tr: "Tüm araştırmacılar", en: "All researchers" })}</option><option value="10">{pick({ tr: "10 araştırmacılık örnek", en: "Sample of 10 researchers" })}</option></select></div><DialogFooter><Button variant="outline" onClick={() => setStartOpen(false)}>{pick({ tr: "Vazgeç", en: "Cancel" })}</Button><Button disabled={blocked || action.isPending} onClick={() => action.mutate({})}>{action.isPending ? pick({ tr: "Sıraya alınıyor…", en: "Queuing…" }) : pick({ tr: "Başlat", en: "Start import" })}</Button></DialogFooter></DialogContent></Dialog>
  </>;
}
