"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BotIcon,
  CheckCircle2Icon,
  CircleSlash2Icon,
  DownloadIcon,
  EyeIcon,
  PlugZapIcon,
  RefreshCwIcon,
  RotateCcwIcon,
} from "lucide-react";
import { toast } from "sonner";
import { useLocale } from "@/components/locale-provider";
import { Detail, EmptyState, ErrorState, LoadingCards, PanelHeader, RatioBar, SearchField, StatusBadge, formatDate } from "@/components/admin/admin-shared";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { adminGet, adminMutate } from "@/lib/admin/client";
import type { AdminPrincipal, AgentRow, AuditEvent, IntegrationOverview } from "@/lib/admin/types";
import { cn } from "@/lib/utils";

type AgentAction = "start" | "stop" | "restart";

export function AgentsPanel({ principal, title, description }: { principal: AdminPrincipal; title: string; description: string }) {
  const { locale, pick } = useLocale();
  const client = useQueryClient();
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [residency, setResidency] = useState("all");
  const [selection, setSelection] = useState<{ agent: AgentRow; action: AgentAction } | null>(null);
  const query = useQuery({ queryKey: ["admin", "agents"], queryFn: () => adminGet<{ items: AgentRow[] }>("agents") });
  const agents = useMemo(() => (query.data?.items ?? []).filter((agent) => {
    const needle = search.trim().toLowerCase();
    const matchesSearch = !needle || agent.email?.toLowerCase().includes(needle) || agent.display_name?.toLowerCase().includes(needle) || agent.user_id.includes(needle);
    const matchesStatus = status === "all" || agent.status === status || (status === "error" && agent.has_error);
    const matchesResidency = residency === "all" || (residency === "resident" ? agent.resident : !agent.resident);
    return matchesSearch && matchesStatus && matchesResidency;
  }), [query.data?.items, residency, search, status]);
  const hasFilters = search || status !== "all" || residency !== "all";

  return (
    <>
      <PanelHeader title={title} description={description} actions={query.data ? <Button variant="outline" size="sm" disabled={query.isFetching} onClick={() => void query.refetch()}><RefreshCwIcon className={cn(query.isFetching && "animate-spin")} />{pick({ tr: "Yenile", en: "Refresh" })}</Button> : undefined} />
      <div className="space-y-4">
        <div className="flex flex-col gap-2 rounded-xl border bg-card/75 p-2.5 shadow-sm lg:flex-row">
          <SearchField value={search} onChange={setSearch} placeholder={pick({ tr: "Ajan, kullanıcı veya e-posta ara", en: "Search agent, user, or email" })} />
          <Select value={status} onValueChange={(value) => setStatus(value ?? "all")}>
            <SelectTrigger className="h-9 w-full bg-card sm:w-44"><SelectValue /></SelectTrigger>
            <SelectContent align="start"><SelectItem value="all">{pick({ tr: "Tüm durumlar", en: "All statuses" })}</SelectItem><SelectItem value="running">{pick({ tr: "Çalışıyor", en: "Running" })}</SelectItem><SelectItem value="stopped">{pick({ tr: "Durduruldu", en: "Stopped" })}</SelectItem><SelectItem value="error">{pick({ tr: "Hata", en: "Error" })}</SelectItem></SelectContent>
          </Select>
          <Select value={residency} onValueChange={(value) => setResidency(value ?? "all")}>
            <SelectTrigger className="h-9 w-full bg-card sm:w-44"><SelectValue /></SelectTrigger>
            <SelectContent align="start"><SelectItem value="all">{pick({ tr: "Tüm çalışma zamanları", en: "All runtimes" })}</SelectItem><SelectItem value="resident">{pick({ tr: "Yerleşik", en: "Resident" })}</SelectItem><SelectItem value="remote">{pick({ tr: "Yerleşik değil", en: "Not resident" })}</SelectItem></SelectContent>
          </Select>
          {hasFilters ? <Button variant="ghost" onClick={() => { setSearch(""); setStatus("all"); setResidency("all"); }}><RotateCcwIcon />{pick({ tr: "Temizle", en: "Clear" })}</Button> : null}
        </div>

        {query.isLoading ? <Skeleton className="h-80 rounded-xl" /> : query.error ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : (
          <Card className="surface-raised border-0 py-0 ring-1 ring-foreground/8">
            <CardContent className="px-0">
              {agents.length ? (
                <>
                  <div className="hidden md:block">
                    <Table>
                      <TableHeader className="bg-muted/45"><TableRow><TableHead className="pl-4">{pick({ tr: "Kullanıcı", en: "User" })}</TableHead><TableHead>{pick({ tr: "Durum", en: "Status" })}</TableHead><TableHead>{pick({ tr: "Çalışma zamanı", en: "Runtime" })}</TableHead><TableHead>{pick({ tr: "Son etkinlik", en: "Last active" })}</TableHead><TableHead className="pr-4 text-right">{pick({ tr: "İşlemler", en: "Actions" })}</TableHead></TableRow></TableHeader>
                      <TableBody>{agents.map((agent) => <TableRow key={agent.user_id}><TableCell className="max-w-80 pl-4"><p className="truncate font-medium">{agent.display_name || agent.email}</p><p className="truncate text-xs text-muted-foreground">{agent.email}</p></TableCell><TableCell><StatusBadge value={agent.has_error ? "error" : agent.status} /></TableCell><TableCell>{agent.resident ? <Badge variant="secondary">{pick({ tr: "Yerleşik", en: "Resident" })}</Badge> : <span className="text-muted-foreground">{pick({ tr: "Uzak", en: "Remote" })}</span>}</TableCell><TableCell className="text-muted-foreground">{formatDate(agent.last_active_at, locale)}</TableCell><TableCell className="pr-4"><AgentButtons agent={agent} principal={principal} onAction={(action) => setSelection({ agent, action })} /></TableCell></TableRow>)}</TableBody>
                    </Table>
                  </div>
                  <div className="divide-y md:hidden">{agents.map((agent) => <div key={agent.user_id} className="space-y-3 p-4"><div><p className="font-medium">{agent.display_name || agent.email}</p><p className="text-xs text-muted-foreground">{agent.email}</p></div><div className="flex flex-wrap gap-2"><StatusBadge value={agent.has_error ? "error" : agent.status} />{agent.resident ? <Badge variant="secondary">{pick({ tr: "Yerleşik", en: "Resident" })}</Badge> : null}</div><p className="text-xs text-muted-foreground">{pick({ tr: "Son etkinlik", en: "Last active" })}: {formatDate(agent.last_active_at, locale)}</p><AgentButtons agent={agent} principal={principal} onAction={(action) => setSelection({ agent, action })} mobile /></div>)}</div>
                </>
              ) : <EmptyState title={pick({ tr: "Eşleşen ajan yok", en: "No matching agents" })} description={pick({ tr: "Filtreleri değiştirerek tekrar dene.", en: "Try changing the filters." })} icon={<BotIcon className="size-4" />} />}
            </CardContent>
          </Card>
        )}
      </div>
      <AgentActionDialog selection={selection} onOpenChange={(open) => !open && setSelection(null)} onDone={() => { setSelection(null); void client.invalidateQueries({ queryKey: ["admin"] }); }} />
    </>
  );
}

function AgentButtons({ agent, principal, onAction, mobile }: { agent: AgentRow; principal: AdminPrincipal; onAction: (action: AgentAction) => void; mobile?: boolean }) {
  const { pick } = useLocale();
  return <div className={cn("flex justify-end gap-1", mobile && "justify-start")}><Button variant="outline" size="sm" onClick={() => onAction("restart")}>{pick({ tr: "Yeniden başlat", en: "Restart" })}</Button>{principal.permissions.includes("agents:manage") ? <Button variant="ghost" size="sm" onClick={() => onAction(agent.status === "stopped" ? "start" : "stop")}>{agent.status === "stopped" ? pick({ tr: "Başlat", en: "Start" }) : pick({ tr: "Durdur", en: "Stop" })}</Button> : null}</div>;
}

function AgentActionDialog({ selection, onOpenChange, onDone }: { selection: { agent: AgentRow; action: AgentAction } | null; onOpenChange: (open: boolean) => void; onDone: () => void }) {
  const { pick } = useLocale();
  const [reason, setReason] = useState("");
  const mutation = useMutation({ mutationFn: () => adminMutate(`agents/${selection!.agent.user_id}/action`, "POST", { action: selection!.action, reason: reason.trim() }), onSuccess: () => { toast.success(pick({ tr: "Ajan işlemi tamamlandı", en: "Agent action completed" })); setReason(""); onDone(); }, onError: (error) => toast.error(error.message) });
  const actionCopy = selection?.action === "restart" ? pick({ tr: "Ajanı yeniden başlat", en: "Restart agent" }) : selection?.action === "start" ? pick({ tr: "Ajanı başlat", en: "Start agent" }) : pick({ tr: "Ajanı durdur", en: "Stop agent" });
  return <Dialog open={Boolean(selection)} onOpenChange={onOpenChange}><DialogContent><DialogHeader><DialogTitle>{actionCopy}</DialogTitle><DialogDescription>{selection?.agent.email ?? selection?.agent.user_id}</DialogDescription></DialogHeader><div className="space-y-2"><Label htmlFor="agent-action-reason">{pick({ tr: "Gerekçe", en: "Reason" })}</Label><Textarea id="agent-action-reason" value={reason} onChange={(event) => setReason(event.target.value)} placeholder={pick({ tr: "Denetim kaydı için kısa bir gerekçe", en: "A short reason for the audit log" })} /></div><DialogFooter><Button variant="outline" onClick={() => onOpenChange(false)}>{pick({ tr: "Vazgeç", en: "Cancel" })}</Button><Button disabled={reason.trim().length < 3 || mutation.isPending} onClick={() => mutation.mutate()}>{mutation.isPending ? pick({ tr: "Uygulanıyor…", en: "Applying…" }) : actionCopy}</Button></DialogFooter></DialogContent></Dialog>;
}

export function IntegrationsPanel({ title, description }: { title: string; description: string }) {
  const { pick } = useLocale();
  const query = useQuery({ queryKey: ["admin", "integrations"], queryFn: () => adminGet<IntegrationOverview>("integrations") });
  return <><PanelHeader title={title} description={description} actions={query.data ? <Badge variant="outline">{query.data.connected_accounts} {pick({ tr: "bağlı hesap", en: "connected accounts" })}</Badge> : undefined} />{query.isLoading ? <LoadingCards count={2} /> : query.error ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : <div className="grid gap-4 md:grid-cols-2">{query.data!.items.map((item) => { const commit = query.data!.commits[item.id]; return <Card key={item.id} className="surface-raised border-0 ring-1 ring-foreground/8"><CardHeader className="border-b"><div className="flex items-start justify-between gap-3"><div><CardTitle>{pick({ tr: item.name_tr, en: item.name_en })}</CardTitle><CardDescription className="mt-1 font-mono text-xs">{item.id}</CardDescription></div>{commit ? <Badge variant="secondary"><CheckCircle2Icon />{pick({ tr: "Kurulu", en: "Installed" })}</Badge> : <Badge variant="outline"><CircleSlash2Icon />{pick({ tr: "Kurulu değil", en: "Not installed" })}</Badge>}</div></CardHeader><CardContent className="space-y-4"><RatioBar value={item.adopted} total={query.data!.connected_accounts} label={pick({ tr: "Benimseyen hesap", en: "Adopted accounts" })} /><div className="flex items-center justify-between rounded-lg border bg-muted/25 p-3"><span className="text-sm text-muted-foreground">{pick({ tr: "Doğrulama hatası", en: "Verification failures" })}</span><span className={cn("font-semibold tabular-nums", item.verification_failures > 0 && "text-destructive")}>{item.verification_failures}</span></div><div className="flex items-center gap-2 text-xs text-muted-foreground"><PlugZapIcon className="size-3.5" /><span className="truncate font-mono">{commit ?? pick({ tr: "Sürüm bilgisi yok", en: "No version information" })}</span></div></CardContent></Card>; })}</div>}</>;
}

export function AuditPanel({ principal, title, description }: { principal: AdminPrincipal; title: string; description: string }) {
  const { locale, pick } = useLocale();
  const [action, setAction] = useState("");
  const [debouncedAction, setDebouncedAction] = useState("");
  const [limit, setLimit] = useState("50");
  const [selected, setSelected] = useState<AuditEvent | null>(null);
  useEffect(() => { const handle = setTimeout(() => setDebouncedAction(action.trim()), 300); return () => clearTimeout(handle); }, [action]);
  const query = useQuery({ queryKey: ["admin", "audit", debouncedAction, limit], queryFn: () => adminGet<{ items: AuditEvent[] }>(`audit?limit=${limit}${debouncedAction ? `&action=${encodeURIComponent(debouncedAction)}` : ""}`) });
  return <>
    <PanelHeader title={title} description={description} actions={principal.permissions.includes("audit:export") ? <Button render={<Link href="/api/admin/audit/export" />} variant="outline"><DownloadIcon />{pick({ tr: "CSV dışa aktar", en: "Export CSV" })}</Button> : undefined} />
    <div className="space-y-4">
      <div className="flex flex-col gap-2 rounded-xl border bg-card/75 p-2.5 shadow-sm sm:flex-row"><SearchField value={action} onChange={setAction} placeholder={pick({ tr: "Tam eylem adına göre filtrele", en: "Filter by exact action name" })} /><Select value={limit} onValueChange={(value) => setLimit(value ?? "50")}><SelectTrigger className="h-9 w-full bg-card sm:w-36"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="50">50 {pick({ tr: "kayıt", en: "records" })}</SelectItem><SelectItem value="100">100 {pick({ tr: "kayıt", en: "records" })}</SelectItem><SelectItem value="200">200 {pick({ tr: "kayıt", en: "records" })}</SelectItem></SelectContent></Select>{action ? <Button variant="ghost" onClick={() => setAction("")}><RotateCcwIcon />{pick({ tr: "Temizle", en: "Clear" })}</Button> : null}</div>
      {query.isLoading ? <Skeleton className="h-80 rounded-xl" /> : query.error ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : <Card className="surface-raised border-0 py-0 ring-1 ring-foreground/8"><CardContent className="px-0">{query.data?.items.length ? <><div className="hidden md:block"><Table><TableHeader className="bg-muted/45"><TableRow><TableHead className="pl-4">{pick({ tr: "Zaman", en: "Time" })}</TableHead><TableHead>{pick({ tr: "Eylem", en: "Action" })}</TableHead><TableHead>{pick({ tr: "Sonuç", en: "Result" })}</TableHead><TableHead>{pick({ tr: "Hedef", en: "Target" })}</TableHead><TableHead className="pr-4 text-right"><span className="sr-only">{pick({ tr: "Ayrıntı", en: "Details" })}</span></TableHead></TableRow></TableHeader><TableBody>{query.data.items.map((event) => <TableRow key={event.id}><TableCell className="pl-4 text-muted-foreground">{formatDate(event.created_at, locale)}</TableCell><TableCell className="font-medium">{event.action}</TableCell><TableCell><StatusBadge value={event.result} /></TableCell><TableCell className="max-w-56 truncate font-mono text-xs">{event.target_user_id ?? "—"}</TableCell><TableCell className="pr-4 text-right"><Button variant="ghost" size="icon-sm" onClick={() => setSelected(event)} aria-label={pick({ tr: "Ayrıntıyı görüntüle", en: "View details" })}><EyeIcon /></Button></TableCell></TableRow>)}</TableBody></Table></div><div className="divide-y md:hidden">{query.data.items.map((event) => <button key={event.id} type="button" className="w-full space-y-2 p-4 text-left hover:bg-muted/35" onClick={() => setSelected(event)}><div className="flex items-center justify-between gap-2"><span className="font-medium">{event.action}</span><StatusBadge value={event.result} /></div><p className="text-xs text-muted-foreground">{formatDate(event.created_at, locale)}</p></button>)}</div></> : <EmptyState title={pick({ tr: "Denetim kaydı bulunamadı", en: "No audit events found" })} description={pick({ tr: "Filtreyi temizleyerek tekrar deneyin.", en: "Clear the filter and try again." })} />}</CardContent></Card>}
    </div>
    <AuditDetailDialog event={selected} onOpenChange={(open) => !open && setSelected(null)} />
  </>;
}

function AuditDetailDialog({ event, onOpenChange }: { event: AuditEvent | null; onOpenChange: (open: boolean) => void }) {
  const { locale, pick } = useLocale();
  return <Dialog open={Boolean(event)} onOpenChange={onOpenChange}><DialogContent className="sm:max-w-2xl"><DialogHeader><DialogTitle>{event?.action}</DialogTitle><DialogDescription>{event ? formatDate(event.created_at, locale) : ""}</DialogDescription></DialogHeader>{event ? <div className="grid gap-3 sm:grid-cols-2"><Detail label={pick({ tr: "Sonuç", en: "Result" })}><StatusBadge value={event.result} /></Detail><Detail label={pick({ tr: "Gerekçe", en: "Reason" })}>{event.reason ?? "—"}</Detail><Detail label={pick({ tr: "Aktör", en: "Actor" })}><span className="break-all font-mono text-xs">{event.actor_user_id}</span></Detail><Detail label={pick({ tr: "Hedef", en: "Target" })}><span className="break-all font-mono text-xs">{event.target_user_id ?? "—"}</span></Detail><Detail label={pick({ tr: "Önce", en: "Before" })} className="sm:col-span-2"><pre className="max-h-44 overflow-auto whitespace-pre-wrap text-xs font-normal">{event.before ? JSON.stringify(event.before, null, 2) : "—"}</pre></Detail><Detail label={pick({ tr: "Sonra", en: "After" })} className="sm:col-span-2"><pre className="max-h-44 overflow-auto whitespace-pre-wrap text-xs font-normal">{event.after ? JSON.stringify(event.after, null, 2) : "—"}</pre></Detail></div> : null}</DialogContent></Dialog>;
}
