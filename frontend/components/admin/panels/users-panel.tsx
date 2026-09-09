"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  MailPlusIcon,
  RotateCcwIcon,
  UserRoundIcon,
} from "lucide-react";
import { toast } from "sonner";
import { useLocale } from "@/components/locale-provider";
import { Detail, EmptyState, ErrorState, PanelHeader, SearchField, StatusBadge, formatDate } from "@/components/admin/admin-shared";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";
import { adminGet, adminMutate } from "@/lib/admin/client";
import type { AdminPrincipal, AdminUser, AdminUserDetail } from "@/lib/admin/types";

type UserAction = "suspend" | "reactivate" | "delete";

export function UsersPanel({ principal, title, description }: { principal: AdminPrincipal; title: string; description: string }) {
  const { pick } = useLocale();
  const client = useQueryClient();
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [cursors, setCursors] = useState<Array<string | null>>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [selected, setSelected] = useState<AdminUser | null>(null);
  const [actionUser, setActionUser] = useState<AdminUser | null>(null);
  const [action, setAction] = useState<UserAction | null>(null);
  const cursor = cursors[pageIndex];

  useEffect(() => {
    const handle = setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => clearTimeout(handle);
  }, [search]);

  const query = useQuery({
    queryKey: ["admin", "users", debouncedSearch, status, cursor],
    queryFn: () => adminGet<{ items: AdminUser[]; next_cursor: string | null }>(
      `users?q=${encodeURIComponent(debouncedSearch)}${status !== "all" ? `&account_status=${status}` : ""}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
    ),
  });
  const hasFilters = Boolean(search || status !== "all");

  function resetFilters() {
    setSearch("");
    setDebouncedSearch("");
    setStatus("all");
    setCursors([null]);
    setPageIndex(0);
  }

  function nextPage() {
    const next = query.data?.next_cursor;
    if (!next) return;
    setCursors((current) => [...current.slice(0, pageIndex + 1), next]);
    setPageIndex((current) => current + 1);
  }

  return (
    <>
      <PanelHeader
        title={title}
        description={description}
        actions={principal.permissions.includes("users:invite") ? (
          <Button onClick={() => setInviteOpen(true)}><MailPlusIcon />{pick({ tr: "Kullanıcı davet et", en: "Invite user" })}</Button>
        ) : undefined}
      />

      <div className="space-y-4">
        <div className="flex flex-col gap-2 rounded-xl border bg-card/75 p-2.5 shadow-sm sm:flex-row">
          <SearchField value={search} onChange={(value) => { setSearch(value); setCursors([null]); setPageIndex(0); }} placeholder={pick({ tr: "Ad veya e-posta ara", en: "Search name or email" })} />
          <Select value={status} onValueChange={(value) => { setStatus(value ?? "all"); setCursors([null]); setPageIndex(0); }}>
            <SelectTrigger className="h-9 w-full bg-card sm:w-44"><SelectValue /></SelectTrigger>
            <SelectContent align="start">
              <SelectItem value="all">{pick({ tr: "Tüm durumlar", en: "All statuses" })}</SelectItem>
              <SelectItem value="active">{pick({ tr: "Etkin", en: "Active" })}</SelectItem>
              <SelectItem value="suspended">{pick({ tr: "Askıya alınmış", en: "Suspended" })}</SelectItem>
            </SelectContent>
          </Select>
          {hasFilters ? <Button variant="ghost" className="sm:w-auto" onClick={resetFilters}><RotateCcwIcon />{pick({ tr: "Temizle", en: "Clear" })}</Button> : null}
          <Button render={<Link href="/api/admin/exports/users" />} variant="outline">CSV</Button>
        </div>

        {query.isLoading ? (
          <Skeleton className="h-80 rounded-xl" />
        ) : query.error ? (
          <ErrorState error={query.error} retry={() => void query.refetch()} />
        ) : (
          <Card className="surface-raised border-0 py-0 ring-1 ring-foreground/8">
            <CardContent className="px-0">
              {query.data?.items.length ? (
                <>
                  <div className="hidden md:block">
                    <Table>
                      <TableHeader className="bg-muted/45">
                        <TableRow>
                          <TableHead className="pl-4">{pick({ tr: "Kullanıcı", en: "User" })}</TableHead>
                          <TableHead>{pick({ tr: "Hesap", en: "Account" })}</TableHead>
                          <TableHead>{pick({ tr: "Ajan", en: "Agent" })}</TableHead>
                          <TableHead>{pick({ tr: "Son görülme", en: "Last seen" })}</TableHead>
                          <TableHead className="pr-4 text-right"><span className="sr-only">{pick({ tr: "İşlemler", en: "Actions" })}</span></TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {query.data.items.map((user) => (
                          <UserTableRow key={user.user_id} user={user} onInspect={() => setSelected(user)} />
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                  <div className="divide-y md:hidden">
                    {query.data.items.map((user) => <UserMobileCard key={user.user_id} user={user} onInspect={() => setSelected(user)} />)}
                  </div>
                </>
              ) : (
                <EmptyState title={pick({ tr: "Eşleşen kullanıcı yok", en: "No matching users" })} description={pick({ tr: "Arama veya durum filtresini değiştirerek tekrar dene.", en: "Try changing the search or status filter." })} icon={<UserRoundIcon className="size-4" />} />
              )}
              <div className="flex items-center justify-between border-t px-3 py-2.5">
                <span className="text-xs text-muted-foreground">{pick({ tr: "Sayfa", en: "Page" })} {pageIndex + 1}</span>
                <div className="flex gap-2">
                  <Button variant="outline" size="sm" disabled={pageIndex === 0 || query.isFetching} onClick={() => setPageIndex((current) => Math.max(0, current - 1))}>
                    <ChevronLeftIcon />{pick({ tr: "Önceki", en: "Previous" })}
                  </Button>
                  <Button variant="outline" size="sm" disabled={!query.data?.next_cursor || query.isFetching} onClick={nextPage}>
                    {pick({ tr: "Sonraki", en: "Next" })}<ChevronRightIcon />
                  </Button>
                </div>
              </div>
            </CardContent>
          </Card>
        )}
      </div>

      <InviteDialog open={inviteOpen} onOpenChange={setInviteOpen} onDone={() => void client.invalidateQueries({ queryKey: ["admin", "users"] })} />
      <UserDialog
        user={selected}
        onOpenChange={(open) => !open && setSelected(null)}
        principal={principal}
        onAction={(nextAction) => { setActionUser(selected); setSelected(null); setAction(nextAction); }}
      />
      <UserActionDialog
        user={actionUser}
        action={action}
        onOpenChange={(open) => { if (!open) { setAction(null); setActionUser(null); } }}
        onDone={() => { setActionUser(null); void client.invalidateQueries({ queryKey: ["admin"] }); }}
      />
    </>
  );
}

function UserTableRow({ user, onInspect }: { user: AdminUser; onInspect: () => void }) {
  const { locale, pick } = useLocale();
  return (
    <TableRow>
      <TableCell className="max-w-80 pl-4">
        <p className="truncate font-medium">{user.display_name || user.email || "—"}</p>
        <p className="truncate text-xs text-muted-foreground">{user.email}</p>
      </TableCell>
      <TableCell><StatusBadge value={user.status} /></TableCell>
      <TableCell><StatusBadge value={user.agent_status} /></TableCell>
      <TableCell className="text-muted-foreground">{formatDate(user.last_seen_at, locale)}</TableCell>
      <TableCell className="pr-4 text-right"><Button variant="ghost" size="sm" onClick={onInspect}>{pick({ tr: "İncele", en: "Inspect" })}</Button></TableCell>
    </TableRow>
  );
}

function UserMobileCard({ user, onInspect }: { user: AdminUser; onInspect: () => void }) {
  const { locale, pick } = useLocale();
  return (
    <div className="space-y-3 p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0"><p className="truncate font-medium">{user.display_name || user.email || "—"}</p><p className="truncate text-xs text-muted-foreground">{user.email}</p></div>
        <Button variant="outline" size="sm" onClick={onInspect}>{pick({ tr: "İncele", en: "Inspect" })}</Button>
      </div>
      <div className="flex flex-wrap gap-2"><StatusBadge value={user.status} /><StatusBadge value={user.agent_status} /></div>
      <p className="text-xs text-muted-foreground">{pick({ tr: "Son görülme", en: "Last seen" })}: {formatDate(user.last_seen_at, locale)}</p>
    </div>
  );
}

function InviteDialog({ open, onOpenChange, onDone }: { open: boolean; onOpenChange: (open: boolean) => void; onDone: () => void }) {
  const { pick } = useLocale();
  const [email, setEmail] = useState("");
  const valid = /^\S+@\S+\.\S+$/.test(email.trim());
  const mutation = useMutation({
    mutationFn: () => adminMutate("invitations", "POST", { email: email.trim() }),
    onSuccess: () => { toast.success(pick({ tr: "Davet gönderildi", en: "Invitation sent" })); onOpenChange(false); setEmail(""); onDone(); },
    onError: (error) => toast.error(error.message),
  });
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>{pick({ tr: "Kullanıcı davet et", en: "Invite user" })}</DialogTitle><DialogDescription>{pick({ tr: "Güvenli davet akışı kullanılır; parola ayarlanmaz veya gösterilmez.", en: "The secure invitation flow is used; no password is set or shown." })}</DialogDescription></DialogHeader>
        <div className="space-y-2"><Label htmlFor="invite-email">{pick({ tr: "E-posta", en: "Email" })}</Label><Input id="invite-email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder={pick({ tr: "ornek@eposta.com", en: "you@example.com" })} /></div>
        <DialogFooter><Button variant="outline" onClick={() => onOpenChange(false)}>{pick({ tr: "Vazgeç", en: "Cancel" })}</Button><Button onClick={() => mutation.mutate()} disabled={!valid || mutation.isPending}>{mutation.isPending ? pick({ tr: "Gönderiliyor…", en: "Sending…" }) : pick({ tr: "Daveti gönder", en: "Send invite" })}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function UserDialog({ user, onOpenChange, principal, onAction }: { user: AdminUser | null; onOpenChange: (open: boolean) => void; principal: AdminPrincipal; onAction: (action: UserAction) => void }) {
  const { locale, pick } = useLocale();
  const query = useQuery({ queryKey: ["admin", "user", user?.user_id], queryFn: () => adminGet<AdminUserDetail>(`users/${user!.user_id}`), enabled: Boolean(user) });
  return (
    <Dialog open={Boolean(user)} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader><DialogTitle>{user?.display_name || user?.email}</DialogTitle><DialogDescription className="break-all font-mono text-xs">{user?.user_id}</DialogDescription></DialogHeader>
        {query.isLoading ? <Skeleton className="h-48" /> : query.error ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : query.data ? (
          <div className="grid gap-3 sm:grid-cols-2">
            <Detail label={pick({ tr: "Hesap durumu", en: "Account status" })}><StatusBadge value={query.data.status} /></Detail>
            <Detail label={pick({ tr: "Son görülme", en: "Last seen" })}>{formatDate(query.data.last_seen_at, locale)}</Detail>
            <Detail label={pick({ tr: "Ajan", en: "Agent" })}>{query.data.agent ? <StatusBadge value={query.data.agent.status} /> : "—"}</Detail>
            <Detail label={pick({ tr: "Oturum sayısı", en: "Session count" })}>{query.data.sessions.count}</Detail>
            <Detail label={pick({ tr: "METU bağlantısı", en: "METU connection" })}>{query.data.campus.connected ? <Badge variant="secondary">{pick({ tr: "Bağlı", en: "Connected" })}</Badge> : "—"}</Detail>
            <Detail label={pick({ tr: "Etkin araçlar", en: "Enabled tools" })} className="sm:col-span-2">{query.data.campus.enabled_tools.join(", ") || "—"}</Detail>
          </div>
        ) : null}
        <DialogFooter>
          {principal.permissions.includes("users:suspend") && user?.status === "active" ? <Button variant="destructive" onClick={() => onAction("suspend")}>{pick({ tr: "Askıya al", en: "Suspend" })}</Button> : null}
          {principal.permissions.includes("users:suspend") && user?.status === "suspended" ? <Button variant="outline" onClick={() => onAction("reactivate")}>{pick({ tr: "Yeniden etkinleştir", en: "Reactivate" })}</Button> : null}
          {principal.permissions.includes("users:delete") && user?.status === "suspended" ? <Button variant="destructive" onClick={() => onAction("delete")}>{pick({ tr: "Kalıcı sil", en: "Delete permanently" })}</Button> : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function UserActionDialog({ user, action, onOpenChange, onDone }: { user: AdminUser | null; action: UserAction | null; onOpenChange: (open: boolean) => void; onDone: () => void }) {
  const { pick } = useLocale();
  const [reason, setReason] = useState("");
  const [confirmEmail, setConfirmEmail] = useState("");
  const mutation = useMutation({
    mutationFn: () => adminMutate(`users/${user!.user_id}/${action === "delete" ? "" : action}`.replace(/\/$/, ""), action === "delete" ? "DELETE" : "POST", action === "delete" ? { reason, confirm_email: confirmEmail } : { reason }),
    onSuccess: () => { toast.success(pick({ tr: "İşlem tamamlandı", en: "Action completed" })); setReason(""); setConfirmEmail(""); onOpenChange(false); onDone(); },
    onError: (error) => toast.error(error.message),
  });
  const deleting = action === "delete";
  const confirmed = !deleting || confirmEmail.trim().toLowerCase() === user?.email?.toLowerCase();
  return (
    <Dialog open={Boolean(user && action)} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{deleting ? pick({ tr: "Kalıcı silme", en: "Permanent deletion" }) : action === "suspend" ? pick({ tr: "Hesabı askıya al", en: "Suspend account" }) : pick({ tr: "Hesabı etkinleştir", en: "Reactivate account" })}</DialogTitle>
          <DialogDescription>{deleting ? pick({ tr: "Bu işlem geri alınamaz. Sunucu güvenlik ön koşullarını tekrar denetler.", en: "This action cannot be undone. The server rechecks all safety preconditions." }) : user?.email}</DialogDescription>
        </DialogHeader>
        <div className="space-y-2"><Label htmlFor="user-action-reason">{pick({ tr: "Gerekçe", en: "Reason" })}</Label><Textarea id="user-action-reason" value={reason} onChange={(event) => setReason(event.target.value)} placeholder={pick({ tr: "Bu işlemin neden gerekli olduğunu yazın", en: "Explain why this action is required" })} /></div>
        {deleting ? <div className="space-y-2"><Label htmlFor="confirm-email">{pick({ tr: "Onaylamak için e-postayı yazın", en: "Type the email to confirm" })}</Label><Input id="confirm-email" value={confirmEmail} onChange={(event) => setConfirmEmail(event.target.value)} placeholder={user?.email ?? ""} /></div> : null}
        <DialogFooter><Button variant="outline" onClick={() => onOpenChange(false)}>{pick({ tr: "Vazgeç", en: "Cancel" })}</Button><Button variant={action === "reactivate" ? "default" : "destructive"} disabled={reason.trim().length < 3 || !confirmed || mutation.isPending} onClick={() => mutation.mutate()}>{mutation.isPending ? pick({ tr: "Uygulanıyor…", en: "Applying…" }) : pick({ tr: "Onayla", en: "Confirm" })}</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
