"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2Icon,
  ExternalLinkIcon,
  RefreshCwIcon,
  SaveIcon,
  ServerCogIcon,
  ShieldCheckIcon,
  Trash2Icon,
  TriangleAlertIcon,
  UserCogIcon,
} from "lucide-react";
import { toast } from "sonner";
import { useLocale } from "@/components/locale-provider";
import { Detail, EmptyState, ErrorState, LoadingCards, PanelHeader, SearchField, StatusBadge, formatDate } from "@/components/admin/admin-shared";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { adminGet, adminMutate } from "@/lib/admin/client";
import type { AdminMembership, AdminPrincipal, AdminRole, AdminUser, RuntimeSettings, SystemHealth } from "@/lib/admin/types";
import { cn } from "@/lib/utils";

export function AccessPanel({ principal, title, description }: { principal: AdminPrincipal; title: string; description: string }) {
  const { pick } = useLocale();
  const client = useQueryClient();
  const [userId, setUserId] = useState("");
  const [selectedLabel, setSelectedLabel] = useState("");
  const [role, setRole] = useState<AdminRole>("campus_admin");
  const [reason, setReason] = useState("");
  const [removing, setRemoving] = useState<AdminMembership | null>(null);
  const query = useQuery({ queryKey: ["admin", "memberships"], queryFn: () => adminGet<{ items: AdminMembership[] }>("memberships") });
  const mutation = useMutation({
    mutationFn: () => adminMutate(`memberships/${userId}`, "PUT", { user_id: userId, role, organization_id: null, reason: reason.trim() }),
    onSuccess: () => { toast.success(pick({ tr: "Yönetici rolü kaydedildi", en: "Admin role saved" })); clearForm(); void client.invalidateQueries({ queryKey: ["admin", "memberships"] }); },
    onError: (error) => toast.error(error.message),
  });

  function clearForm() {
    setUserId(""); setSelectedLabel(""); setRole("campus_admin"); setReason("");
  }

  function editMembership(item: AdminMembership) {
    setUserId(item.user_id);
    setSelectedLabel(item.email ?? item.user_id);
    setRole(item.role);
    setReason("");
  }

  return <>
    <PanelHeader title={title} description={description} />
    <div className="grid gap-4 xl:grid-cols-[1fr_380px]">
      <Card className="surface-raised border-0 ring-1 ring-foreground/8">
        <CardHeader className="border-b"><CardTitle>{pick({ tr: "Yönetici üyelikleri", en: "Admin memberships" })}</CardTitle><CardDescription>{pick({ tr: "Rolleri düzenleyin veya korumasız üyelikleri kaldırın.", en: "Edit roles or remove unprotected memberships." })}</CardDescription></CardHeader>
        <CardContent className="px-0">
          {query.isLoading ? <div className="p-4"><Skeleton className="h-52" /></div> : query.error ? <div className="p-4"><ErrorState error={query.error} retry={() => void query.refetch()} /></div> : query.data?.items.length ? <div className="divide-y">{query.data.items.map((item) => { const protectedMembership = item.bootstrap || item.user_id === principal.user_id; return <div key={item.user_id} className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between"><div className="min-w-0"><p className="truncate font-medium">{item.email ?? item.user_id}</p><p className="mt-0.5 truncate font-mono text-[11px] text-muted-foreground">{item.user_id}</p></div><div className="flex flex-wrap items-center gap-2"><StatusBadge value={item.role} />{item.bootstrap ? <Badge variant="outline">bootstrap</Badge> : null}<Button variant="outline" size="sm" disabled={protectedMembership} onClick={() => editMembership(item)}>{pick({ tr: "Düzenle", en: "Edit" })}</Button><Button variant="ghost" size="icon-sm" className="text-destructive" disabled={protectedMembership} onClick={() => setRemoving(item)} aria-label={pick({ tr: "Yönetici erişimini kaldır", en: "Remove admin access" })}><Trash2Icon /></Button></div></div>; })}</div> : <EmptyState title={pick({ tr: "Yönetici üyeliği yok", en: "No admin memberships" })} icon={<ShieldCheckIcon className="size-4" />} />}
        </CardContent>
      </Card>

      <Card className="surface-raised h-fit border-0 ring-1 ring-foreground/8 xl:sticky xl:top-4">
        <CardHeader className="border-b"><CardTitle>{userId ? pick({ tr: "Rolü güncelle", en: "Update role" }) : pick({ tr: "Rol ata", en: "Assign role" })}</CardTitle><CardDescription>{pick({ tr: "Kullanıcı seçin, rolü belirleyin ve denetim gerekçesi ekleyin.", en: "Choose a user, set a role, and add an audit reason." })}</CardDescription></CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); mutation.mutate(); }}>
            <div className="space-y-2"><Label htmlFor="member-search">{pick({ tr: "Kullanıcı", en: "User" })}</Label><UserPicker id="member-search" selectedLabel={selectedLabel} onSelect={(user) => { setUserId(user.user_id); setSelectedLabel(user.email ?? user.display_name ?? user.user_id); }} onClear={clearForm} /></div>
            <div className="space-y-2"><Label>{pick({ tr: "Rol", en: "Role" })}</Label><Select value={role} onValueChange={(value) => setRole((value ?? "campus_admin") as AdminRole)}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent align="start"><SelectItem value="campus_admin">{pick({ tr: "Kampüs yöneticisi", en: "Campus admin" })}</SelectItem><SelectItem value="operator">{pick({ tr: "Operatör", en: "Operator" })}</SelectItem><SelectItem value="super_admin">{pick({ tr: "Süper yönetici", en: "Super admin" })}</SelectItem></SelectContent></Select></div>
            <div className="space-y-2"><Label htmlFor="member-reason">{pick({ tr: "Gerekçe", en: "Reason" })}</Label><Textarea id="member-reason" value={reason} onChange={(event) => setReason(event.target.value)} /></div>
            <div className="flex justify-end gap-2">{userId ? <Button type="button" variant="ghost" onClick={clearForm}>{pick({ tr: "Temizle", en: "Clear" })}</Button> : null}<Button type="submit" disabled={!userId || reason.trim().length < 3 || mutation.isPending}><UserCogIcon />{mutation.isPending ? pick({ tr: "Kaydediliyor…", en: "Saving…" }) : pick({ tr: "Rolü kaydet", en: "Save role" })}</Button></div>
          </form>
        </CardContent>
      </Card>
    </div>
    <RemoveMembershipDialog membership={removing} onOpenChange={(open) => !open && setRemoving(null)} onDone={() => { setRemoving(null); void client.invalidateQueries({ queryKey: ["admin", "memberships"] }); }} />
  </>;
}

function UserPicker({ id, selectedLabel, onSelect, onClear }: { id: string; selectedLabel: string; onSelect: (user: AdminUser) => void; onClear: () => void }) {
  const { pick } = useLocale();
  const [term, setTerm] = useState("");
  const [debounced, setDebounced] = useState("");
  const [open, setOpen] = useState(false);
  useEffect(() => { const handle = setTimeout(() => setDebounced(term), 300); return () => clearTimeout(handle); }, [term]);
  const query = useQuery({ queryKey: ["admin", "user-search", debounced], queryFn: () => adminGet<{ items: AdminUser[] }>(`users?q=${encodeURIComponent(debounced)}`), enabled: debounced.trim().length >= 2 });
  if (selectedLabel) return <div className="flex items-center justify-between gap-2 rounded-lg border bg-muted/30 px-3 py-2 text-sm"><span className="min-w-0 truncate font-medium">{selectedLabel}</span><Button type="button" variant="ghost" size="sm" onClick={onClear}>{pick({ tr: "Değiştir", en: "Change" })}</Button></div>;
  return <div className="relative"><SearchField value={term} onChange={(value) => { setTerm(value); setOpen(true); }} placeholder={pick({ tr: "Ad veya e-posta ara", en: "Search name or email" })} /><input id={id} className="sr-only" aria-hidden tabIndex={-1} />{open && debounced.trim().length >= 2 ? <div className="absolute z-20 mt-1 w-full overflow-hidden rounded-lg border bg-popover shadow-lg">{query.isLoading ? <p className="p-3 text-sm text-muted-foreground">{pick({ tr: "Aranıyor…", en: "Searching…" })}</p> : !query.data?.items.length ? <p className="p-3 text-sm text-muted-foreground">{pick({ tr: "Eşleşen kullanıcı yok.", en: "No matching users." })}</p> : query.data.items.slice(0, 8).map((user) => <button key={user.user_id} type="button" className="flex w-full flex-col items-start px-3 py-2 text-left text-sm hover:bg-muted/60 focus:bg-muted/60 focus:outline-none" onMouseDown={(event) => event.preventDefault()} onClick={() => { onSelect(user); setTerm(""); setOpen(false); }}><span className="font-medium">{user.display_name || user.email || user.user_id}</span>{user.email ? <span className="text-xs text-muted-foreground">{user.email}</span> : null}</button>)}</div> : null}</div>;
}

function RemoveMembershipDialog({ membership, onOpenChange, onDone }: { membership: AdminMembership | null; onOpenChange: (open: boolean) => void; onDone: () => void }) {
  const { pick } = useLocale();
  const [reason, setReason] = useState("");
  const mutation = useMutation({ mutationFn: () => adminMutate(`memberships/${membership!.user_id}`, "DELETE", { reason: reason.trim() }), onSuccess: () => { toast.success(pick({ tr: "Yönetici erişimi kaldırıldı", en: "Admin access removed" })); setReason(""); onDone(); }, onError: (error) => toast.error(error.message) });
  return <Dialog open={Boolean(membership)} onOpenChange={onOpenChange}><DialogContent><DialogHeader><DialogTitle>{pick({ tr: "Yönetici erişimini kaldır", en: "Remove admin access" })}</DialogTitle><DialogDescription>{membership?.email ?? membership?.user_id}</DialogDescription></DialogHeader><div className="space-y-2"><Label htmlFor="remove-membership-reason">{pick({ tr: "Gerekçe", en: "Reason" })}</Label><Textarea id="remove-membership-reason" value={reason} onChange={(event) => setReason(event.target.value)} /></div><DialogFooter><Button variant="outline" onClick={() => onOpenChange(false)}>{pick({ tr: "Vazgeç", en: "Cancel" })}</Button><Button variant="destructive" disabled={reason.trim().length < 3 || mutation.isPending} onClick={() => mutation.mutate()}>{mutation.isPending ? pick({ tr: "Kaldırılıyor…", en: "Removing…" }) : pick({ tr: "Erişimi kaldır", en: "Remove access" })}</Button></DialogFooter></DialogContent></Dialog>;
}

export function RuntimePanel({ title, description }: { title: string; description: string }) {
  const query = useQuery({ queryKey: ["admin", "runtime"], queryFn: () => adminGet<RuntimeSettings>("runtime-settings") });
  return <><PanelHeader title={title} description={description} />{query.isLoading ? <Skeleton className="h-[34rem] rounded-xl" /> : query.error ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : <RuntimeForm key={`${query.data!.revision}-${query.data!.updated_at}`} initial={query.data!} />}</>;
}

function RuntimeForm({ initial }: { initial: RuntimeSettings }) {
  const { locale, pick } = useLocale();
  const client = useQueryClient();
  const [form, setForm] = useState(initial);
  const [reason, setReason] = useState("");
  const editableFields = useMemo(() => ({ model_id: form.model_id, profile: form.profile, max_tokens: form.max_tokens, legacy_history_runs: form.legacy_history_runs, scholar_history_runs: form.scholar_history_runs, tool_call_limit: form.tool_call_limit, learning_enabled: form.learning_enabled, input_token_price: form.input_token_price, output_token_price: form.output_token_price }), [form]);
  const initialFields = useMemo(() => ({ model_id: initial.model_id, profile: initial.profile, max_tokens: initial.max_tokens, legacy_history_runs: initial.legacy_history_runs, scholar_history_runs: initial.scholar_history_runs, tool_call_limit: initial.tool_call_limit, learning_enabled: initial.learning_enabled, input_token_price: initial.input_token_price, output_token_price: initial.output_token_price }), [initial]);
  const dirty = JSON.stringify(editableFields) !== JSON.stringify(initialFields);
  const valid = form.model_id.trim().length >= 2 && form.max_tokens >= 256 && form.max_tokens <= 131072 && form.legacy_history_runs >= 0 && form.legacy_history_runs <= 50 && form.scholar_history_runs >= 0 && form.scholar_history_runs <= 50 && form.tool_call_limit >= 1 && form.tool_call_limit <= 50 && form.input_token_price >= 0 && form.input_token_price <= 1 && form.output_token_price >= 0 && form.output_token_price <= 1;
  const mutation = useMutation({ mutationFn: () => adminMutate<RuntimeSettings>("runtime-settings", "PUT", { ...editableFields, reason: reason.trim() }), onSuccess: (data) => { toast.success(pick({ tr: "Ajan varsayılanları güncellendi", en: "Agent defaults updated" })); setReason(""); setForm(data); void client.invalidateQueries({ queryKey: ["admin"] }); }, onError: (error) => toast.error(error.message) });
  const numberField = (key: keyof Pick<RuntimeSettings, "max_tokens" | "legacy_history_runs" | "scholar_history_runs" | "tool_call_limit" | "input_token_price" | "output_token_price">, value: number) => setForm((current) => ({ ...current, [key]: value }));
  return <div className="grid gap-4 xl:grid-cols-[1fr_360px]">
    <Card className="surface-raised border-0 ring-1 ring-foreground/8"><CardHeader className="border-b"><div className="flex items-start justify-between gap-3"><div><CardTitle>{pick({ tr: "Çalışma zamanı varsayılanları", en: "Runtime defaults" })}</CardTitle><CardDescription className="mt-1">{pick({ tr: "Kaydetmek yerleşik ajanları güvenle emekliye ayırır; sonraki istek güncel ayarlarla yeniden oluşturur.", en: "Saving safely retires resident agents; the next request rebuilds them with the new defaults." })}</CardDescription></div>{dirty ? <Badge>{pick({ tr: "Kaydedilmemiş", en: "Unsaved" })}</Badge> : <Badge variant="outline">{pick({ tr: "Güncel", en: "Up to date" })}</Badge>}</div></CardHeader><CardContent><form className="space-y-5" onSubmit={(event: FormEvent) => { event.preventDefault(); mutation.mutate(); }}>
      <fieldset disabled={!form.editable} className="space-y-5 disabled:opacity-65"><div><p className="mb-3 text-xs font-semibold tracking-[0.12em] text-muted-foreground uppercase">{pick({ tr: "Model", en: "Model" })}</p><div className="grid gap-4 sm:grid-cols-2"><div className="space-y-2 sm:col-span-2"><Label htmlFor="model-id">{pick({ tr: "Varsayılan model", en: "Default model" })}</Label><Input id="model-id" value={form.model_id} onChange={(event) => setForm({ ...form, model_id: event.target.value })} minLength={2} /></div><div className="space-y-2"><Label>{pick({ tr: "Profil", en: "Profile" })}</Label><Select value={form.profile} onValueChange={(value) => setForm({ ...form, profile: (value ?? "scholar") as RuntimeSettings["profile"] })}><SelectTrigger className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="scholar">scholar</SelectItem><SelectItem value="legacy">legacy</SelectItem></SelectContent></Select></div><NumberField id="max-tokens" label="Max tokens" value={form.max_tokens} min={256} max={131072} onChange={(value) => numberField("max_tokens", value)} /></div></div>
      <div className="border-t pt-5"><p className="mb-3 text-xs font-semibold tracking-[0.12em] text-muted-foreground uppercase">{pick({ tr: "Geçmiş ve araç sınırları", en: "History and tool limits" })}</p><div className="grid gap-4 sm:grid-cols-3"><NumberField id="scholar-history" label="Scholar history" value={form.scholar_history_runs} min={0} max={50} onChange={(value) => numberField("scholar_history_runs", value)} /><NumberField id="legacy-history" label="Legacy history" value={form.legacy_history_runs} min={0} max={50} onChange={(value) => numberField("legacy_history_runs", value)} /><NumberField id="tool-limit" label="Tool calls" value={form.tool_call_limit} min={1} max={50} onChange={(value) => numberField("tool_call_limit", value)} /></div></div>
      <div className="border-t pt-5"><p className="mb-3 text-xs font-semibold tracking-[0.12em] text-muted-foreground uppercase">{pick({ tr: "Maliyet ve öğrenme", en: "Cost and learning" })}</p><div className="grid gap-4 sm:grid-cols-2"><NumberField id="input-token-price" label={pick({ tr: "Girdi (USD / 1M token)", en: "Input (USD / 1M tokens)" })} value={form.input_token_price * 1_000_000} min={0} max={1_000_000} step="0.01" onChange={(value) => numberField("input_token_price", value / 1_000_000)} /><NumberField id="output-token-price" label={pick({ tr: "Çıktı (USD / 1M token)", en: "Output (USD / 1M tokens)" })} value={form.output_token_price * 1_000_000} min={0} max={1_000_000} step="0.01" onChange={(value) => numberField("output_token_price", value / 1_000_000)} /><label className="flex items-center justify-between gap-3 rounded-xl border bg-muted/25 p-3 sm:col-span-2"><span><span className="block font-medium">{pick({ tr: "Öğrenme etkin", en: "Learning enabled" })}</span><span className="mt-0.5 block text-xs text-muted-foreground">{pick({ tr: "Yeni ajanların kalıcı, hassas olmayan tercihleri öğrenmesine izin verir.", en: "Allows new agents to learn durable, non-sensitive preferences." })}</span></span><Switch checked={form.learning_enabled} onCheckedChange={(checked) => setForm({ ...form, learning_enabled: checked })} /></label></div></div></fieldset>
      {form.editable ? <><div className="space-y-2"><Label htmlFor="runtime-reason">{pick({ tr: "Değişiklik gerekçesi", en: "Change reason" })}</Label><Textarea id="runtime-reason" value={reason} onChange={(event) => setReason(event.target.value)} /></div><div className="flex justify-end gap-2"><Button type="button" variant="ghost" disabled={!dirty || mutation.isPending} onClick={() => { setForm(initial); setReason(""); }}>{pick({ tr: "Değişiklikleri geri al", en: "Reset changes" })}</Button><Button type="submit" disabled={!dirty || !valid || reason.trim().length < 3 || mutation.isPending}><SaveIcon />{mutation.isPending ? pick({ tr: "Uygulanıyor…", en: "Applying…" }) : pick({ tr: "Varsayılanları uygula", en: "Apply defaults" })}</Button></div></> : null}
    </form></CardContent></Card>
    <Card className="surface-raised h-fit border-0 ring-1 ring-foreground/8 xl:sticky xl:top-4"><CardHeader className="border-b"><CardTitle>{pick({ tr: "Etkin yapılandırma", en: "Effective configuration" })}</CardTitle></CardHeader><CardContent className="space-y-3"><Detail label={pick({ tr: "Revizyon", en: "Revision" })}>{form.revision}</Detail><Detail label={pick({ tr: "Kaynak", en: "Source" })}>{form.has_database_override ? pick({ tr: "Veritabanı geçersiz kılması", en: "Database override" }) : pick({ tr: "Ortam varsayılanları", en: "Environment defaults" })}</Detail><Detail label={pick({ tr: "Son güncelleme", en: "Updated" })}>{formatDate(form.updated_at, locale)}</Detail><p className="text-xs leading-5 text-muted-foreground">{pick({ tr: "API anahtarları ve sağlayıcı uç noktaları sunucu ortamında kalır ve burada gösterilmez.", en: "API keys and provider endpoints remain in the server environment and are never shown here." })}</p></CardContent></Card>
  </div>;
}

function NumberField({ id, label, value, min, max, step, onChange }: { id: string; label: string; value: number; min: number; max: number; step?: string; onChange: (value: number) => void }) { return <div className="space-y-2"><Label htmlFor={id}>{label}</Label><Input id={id} type="number" min={min} max={max} step={step} value={Number.isFinite(value) ? value : ""} onChange={(event) => onChange(Number(event.target.value))} /></div>; }

export function SystemPanel({ principal, title, description }: { principal: AdminPrincipal; title: string; description: string }) {
  const { locale, pick } = useLocale();
  const client = useQueryClient();
  const query = useQuery({ queryKey: ["admin", "system"], queryFn: () => adminGet<SystemHealth>("system"), refetchInterval: 30_000, refetchIntervalInBackground: false });
  const sync = useMutation({ mutationFn: () => adminMutate("directory/sync", "POST", {}), onSuccess: () => { toast.success(pick({ tr: "Hesap dizini eşitlendi", en: "Account directory synced" })); void client.invalidateQueries({ queryKey: ["admin"] }); }, onError: (error) => toast.error(error.message) });
  return <><PanelHeader title={title} description={description} actions={query.data ? <><span className="text-xs text-muted-foreground">{formatDate(query.data.checked_at, locale)}</span><Button variant="outline" size="sm" onClick={() => void query.refetch()} disabled={query.isFetching}><RefreshCwIcon className={cn(query.isFetching && "animate-spin")} />{pick({ tr: "Yenile", en: "Refresh" })}</Button></> : undefined} />{query.isLoading ? <LoadingCards /> : query.error ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : <SystemContent data={query.data!} principal={principal} sync={() => sync.mutate()} syncing={sync.isPending} />}</>;
}

function SystemContent({ data, principal, sync, syncing }: { data: SystemHealth; principal: AdminPrincipal; sync: () => void; syncing: boolean }) {
  const { locale, pick } = useLocale();
  const health = [{ label: "Broker", value: data.broker }, { label: "Database", value: data.database }, { label: "PostHog", value: data.posthog }, { label: "Supabase Admin", value: data.supabase_admin }];
  const capacity = data.pool_capacity > 0 ? Math.round((data.resident_agents / data.pool_capacity) * 100) : 0;
  return <div className="space-y-4">
    <Card className="border-primary/18 bg-primary/[0.045]"><CardContent className="flex flex-col justify-between gap-4 sm:flex-row sm:items-center"><div><div className="flex items-center gap-2 font-semibold"><ExternalLinkIcon className="size-4 text-primary" />PostHog</div><p className="mt-1 max-w-3xl text-sm leading-6 text-muted-foreground">{pick({ tr: "LLM kullanımı, maliyet, hata, iz ve değerlendirme analitiği PostHog'da görüntülenir; bu panel bunları çoğaltmaz.", en: "LLM usage, cost, errors, traces, and evaluation analytics live in PostHog; this panel does not duplicate them." })}</p></div>{data.posthog_dashboard_url ? <Button render={<a href={data.posthog_dashboard_url} target="_blank" rel="noreferrer" />}><ExternalLinkIcon />{pick({ tr: "PostHog'u aç", en: "Open PostHog" })}</Button> : <Badge variant="outline">{pick({ tr: "Panel URL'si yok", en: "No dashboard URL" })}</Badge>}</CardContent></Card>
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{health.map((item) => <HealthCard key={item.label} label={item.label} value={item.value} />)}</div>
    <div className="grid gap-4 xl:grid-cols-2"><Card className="surface-raised border-0 ring-1 ring-foreground/8"><CardHeader className="border-b"><CardTitle>{pick({ tr: "Çalışma zamanı havuzu", en: "Runtime pool" })}</CardTitle><CardDescription>{pick({ tr: "Bu brokerda şu anda yerleşik olan ajan kapasitesi.", en: "Agent capacity currently resident on this broker." })}</CardDescription></CardHeader><CardContent className="space-y-4"><div className="flex items-end justify-between"><div><p className="text-3xl font-semibold tabular-nums">{data.resident_agents}<span className="text-base font-normal text-muted-foreground"> / {data.pool_capacity}</span></p><p className="mt-1 text-sm text-muted-foreground">{data.agent_runtime}</p></div><Badge variant="outline">{capacity}%</Badge></div><Progress value={capacity} className="gap-0" aria-label={`${capacity}%`} /><Detail label={pick({ tr: "Son kontrol", en: "Checked at" })}>{formatDate(data.checked_at, locale)}</Detail></CardContent></Card><Card className="surface-raised border-0 ring-1 ring-foreground/8"><CardHeader className="border-b"><CardTitle>{pick({ tr: "Hesap dizini", en: "Account directory" })}</CardTitle><CardDescription>{pick({ tr: "Kimlik dizini normalde beş dakikada bir ve oturum açılmış isteklerde güncellenir.", en: "The identity directory normally refreshes every five minutes and on authenticated requests." })}</CardDescription></CardHeader><CardContent><div className="flex min-h-28 flex-col items-start justify-between gap-4 rounded-xl border bg-muted/25 p-4 sm:flex-row sm:items-center"><div className="flex gap-3"><span className="grid size-10 shrink-0 place-items-center rounded-xl bg-primary/10 text-primary"><ServerCogIcon className="size-4" /></span><div><p className="font-medium">{pick({ tr: "Dizin eşitleme", en: "Directory sync" })}</p><p className="mt-1 text-xs text-muted-foreground">{pick({ tr: "Yetkili hesapları Supabase kimlik diziniyle uzlaştırır.", en: "Reconciles authorized accounts with the Supabase identity directory." })}</p></div></div>{principal.permissions.includes("directory:sync") ? <Button variant="outline" onClick={sync} disabled={syncing}><RefreshCwIcon className={cn(syncing && "animate-spin")} />{syncing ? pick({ tr: "Eşitleniyor…", en: "Syncing…" }) : pick({ tr: "Şimdi eşitle", en: "Sync now" })}</Button> : <Badge variant="outline">{pick({ tr: "Süper yönetici gerekir", en: "Super admin required" })}</Badge>}</div></CardContent></Card></div>
  </div>;
}

function HealthCard({ label, value }: { label: string; value: string }) {
  const { locale } = useLocale();
  const good = value === "ok" || value === "configured";
  return <Card className="surface-raised border-0 ring-1 ring-foreground/8"><CardContent><div className="flex items-center justify-between"><div><p className="text-sm text-muted-foreground">{label}</p><p className="mt-2 font-semibold">{value.replaceAll("_", " ")}</p></div><span className={cn("grid size-9 place-items-center rounded-xl", good ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-300" : "bg-amber-500/10 text-amber-600 dark:text-amber-300")}>{good ? <CheckCircle2Icon className="size-4" aria-label={locale === "tr" ? "Sağlıklı" : "Healthy"} /> : <TriangleAlertIcon className="size-4" aria-label={locale === "tr" ? "Dikkat" : "Attention"} />}</span></div></CardContent></Card>;
}
