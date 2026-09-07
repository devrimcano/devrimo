"use client";

import { useId, useState, type ReactNode } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { useLocale } from "@/components/locale-provider";
import {
  EmptyState,
  ErrorState,
  StatusBadge,
  statusLabel,
  formatDate,
} from "@/components/admin/admin-shared";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { adminGet, adminMutate } from "@/lib/admin/client";
import type {
  KnowledgeSource,
  KnowledgeSourceDetail,
  IngestionJob,
  CourseGroup,
  SourceRevision,
} from "@/lib/admin/types";

export function knowledgeLabel(value: string, locale: "tr" | "en") {
  const names: Record<string, [string, string]> = {
    drupal: ["Web sitesi (Drupal)", "Website (Drupal)"],
    html_page: ["Web sayfası", "Web page"],
    html_table: ["Web tablosu", "Web table"],
    rss: ["Haber akışı (RSS)", "News feed (RSS)"],
    ical: ["Takvim (iCal)", "Calendar (iCal)"],
    json: ["Yapılandırılmış veri (JSON)", "Structured data (JSON)"],
    pdf: ["PDF belgesi", "PDF document"],
    curated: ["Elle hazırlanan içerik", "Manually curated content"],
    approved_social: ["Onaylı sosyal kaynak", "Approved social source"],
    announcement: ["Duyuru", "Announcement"],
    calendar: ["Takvim", "Calendar"],
    event: ["Etkinlik", "Event"],
    service_status: ["Hizmet durumu", "Service status"],
    guide: ["Rehber", "Guide"],
    course: ["Ders", "Course"],
    policy: ["Yönerge", "Policy"],
  };
  return names[value]?.[locale === "tr" ? 0 : 1] ?? value.replaceAll("_", " ");
}

function hasDraft(source: KnowledgeSource) {
  return (source.draft_revisions ?? (source.status === "draft" ? 1 : 0)) > 0;
}

export function FilterSelect({
  label,
  value,
  onChange,
  children,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <div className="min-w-0 space-y-1.5">
      <label htmlFor={id} className="text-xs font-medium text-muted-foreground">
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="h-9 w-full rounded-lg border bg-background px-3 text-sm focus-visible:outline-2 focus-visible:outline-primary"
      >
        {children}
      </select>
    </div>
  );
}

export function SectionNav({
  value,
  onChange,
  items,
}: {
  value: string;
  onChange: (id: string) => void;
  items: { id: string; label: string }[];
}) {
  const { pick } = useLocale();
  return (
    <nav
      aria-label={pick({ tr: "Bilgi görünümleri", en: "Knowledge views" })}
      className="flex gap-1 overflow-x-auto border-b pb-2"
    >
      {items.map((item) => (
        <Button
          key={item.id}
          variant={value === item.id ? "secondary" : "ghost"}
          aria-current={value === item.id ? "page" : undefined}
          onClick={() => onChange(item.id)}
          className="shrink-0"
        >
          {item.label}
        </Button>
      ))}
    </nav>
  );
}

export function sourceHealth(source: KnowledgeSource, now: number) {
  if (!source.enabled) return "disabled";
  if (source.last_error) return "failed";
  if (!source.last_success_at) return "not_ingested";
  return now - new Date(source.last_success_at).getTime() >
    source.schedule_seconds * 2000
    ? "stale"
    : "healthy";
}

export function SourcesBrowser({
  items,
  canWrite,
  selected,
  onSelection,
  onManage,
  bulk,
}: {
  items: KnowledgeSource[];
  canWrite: boolean;
  selected: Set<string>;
  onSelection: (ids: Set<string>) => void;
  onManage: (source: KnowledgeSource) => void;
  bulk: ReactNode;
}) {
  const { pick, locale } = useLocale();
  const [query, setQuery] = useState("");
  const [showFilters, setShowFilters] = useState(false);
  const [status, setStatus] = useState("all");
  const [kind, setKind] = useState("all");
  const [language, setLanguage] = useState("all");
  const [sort, setSort] = useState("name");
  const [page, setPage] = useState(1);
  const now = new Date().getTime();
  const attention = (s: KnowledgeSource) =>
    s.enabled &&
    s.status === "published" &&
    ["failed", "stale", "not_ingested"].includes(sourceHealth(s, now));
  const filtered = items
    .filter(
      (s) =>
        (!query ||
          `${s.name} ${s.url ?? ""}`
            .toLocaleLowerCase(locale)
            .includes(query.toLocaleLowerCase(locale))) &&
        (status === "all" ||
          (status === "attention"
            ? attention(s)
            : status === "indexed"
              ? (s.records ?? 0) > 0
              : status === "disabled"
                ? !s.enabled
                : status === "draft"
                  ? hasDraft(s)
                  : s.status === status)) &&
        (kind === "all" || s.kind === kind) &&
        (language === "all" || s.language === language),
    )
    .sort((a, b) =>
      sort === "name"
        ? a.name.localeCompare(b.name, locale)
        : sort === "records"
          ? (b.records ?? 0) - (a.records ?? 0)
          : (new Date(a.last_success_at ?? 0).getTime() -
              new Date(b.last_success_at ?? 0).getTime()) *
            (sort === "newest" ? -1 : 1),
    );
  const pages = Math.max(1, Math.ceil(filtered.length / 15));
  const currentPage = Math.min(page, pages);
  const visible = filtered.slice((currentPage - 1) * 15, currentPage * 15);
  const checked =
    visible.length > 0 && visible.every((s) => selected.has(s.id));
  const mixed = !checked && visible.some((s) => selected.has(s.id));
  const changeFilter = (fn: (v: string) => void) => (v: string) => {
    fn(v);
    setPage(1);
  };
  function toggle(ids: string[], remove: boolean) {
    const next = new Set(selected);
    ids.forEach((id) => (remove ? next.delete(id) : next.add(id)));
    onSelection(next);
  }
  const summary = [
    {
      id: "published",
      label: pick({ tr: "Yayında", en: "Published" }),
      count: items.filter((s) => s.status === "published").length,
    },
    {
      id: "draft",
      label: pick({ tr: "Taslaklar", en: "Drafts" }),
      count: items.filter(hasDraft).length,
    },
    {
      id: "attention",
      label: pick({ tr: "İlgilenilmesi gereken", en: "Needs attention" }),
      count: items.filter(attention).length,
    },
    {
      id: "indexed",
      label: pick({ tr: "İndekslenen kayıtlar", en: "Indexed records" }),
      count: items.reduce((n, s) => n + (s.records ?? 0), 0),
    },
  ];
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
        {summary.map((item) => (
          <button
            key={item.id}
            onClick={() => {
              setStatus(item.id);
              setKind("all");
              setLanguage("all");
              setQuery("");
              setPage(1);
            }}
            aria-pressed={status === item.id}
            className={`rounded-xl border p-4 text-left transition-colors hover:bg-muted/60 focus-visible:outline-2 focus-visible:outline-primary ${status === item.id ? "border-primary bg-primary/5" : "bg-card"}`}
          >
            <span className="text-xs text-muted-foreground">{item.label}</span>
            <span className="mt-2 block text-3xl font-semibold tabular-nums">
              {item.count}
            </span>
          </button>
        ))}
      </div>
      <div className="space-y-3">
        <div className="space-y-1.5">
          <label
            htmlFor="source-search"
            className="text-xs font-medium text-muted-foreground"
          >
            {pick({ tr: "Kaynak ara", en: "Find a source" })}
          </label>
          <Input
            id="source-search"
            placeholder={pick({ tr: "Ad veya URL", en: "Name or URL" })}
            value={query}
            onChange={(e) => changeFilter(setQuery)(e.target.value)}
          />
        </div>
        <Button
          className="sm:hidden"
          variant="outline"
          aria-expanded={showFilters}
          aria-controls="source-filters"
          onClick={() => setShowFilters((v) => !v)}
        >
          {pick({ tr: "Filtreler ve sıralama", en: "Filters and sorting" })}
        </Button>
        <div
          id="source-filters"
          className={`${showFilters ? "grid" : "hidden"} gap-3 sm:grid sm:grid-cols-4`}
        >
          <FilterSelect
            label={pick({ tr: "Durum", en: "Status" })}
            value={status}
            onChange={changeFilter(setStatus)}
          >
            {[
              ["all", "Tüm kaynaklar", "All sources"],
              ["published", "Yayında", "Published"],
              ["draft", "Taslak", "Draft"],
              ["attention", "İlgilenilmesi gereken", "Needs attention"],
              ["disabled", "Devre dışı", "Disabled"],
              ["indexed", "Kayıt içeren", "With records"],
            ].map(([v, tr, en]) => (
              <option key={v} value={v}>
                {pick({ tr, en })}
              </option>
            ))}
          </FilterSelect>
          <FilterSelect
            label={pick({ tr: "Tür", en: "Type" })}
            value={kind}
            onChange={changeFilter(setKind)}
          >
            <option value="all">
              {pick({ tr: "Tüm türler", en: "All types" })}
            </option>
            {[...new Set(items.map((s) => s.kind))].sort().map((k) => (
              <option key={k} value={k}>
                {knowledgeLabel(k, locale)}
              </option>
            ))}
          </FilterSelect>
          <FilterSelect
            label={pick({ tr: "Dil", en: "Language" })}
            value={language}
            onChange={changeFilter(setLanguage)}
          >
            <option value="all">
              {pick({ tr: "Tüm diller", en: "All languages" })}
            </option>
            <option value="tr">Türkçe</option>
            <option value="en">English</option>
          </FilterSelect>
          <FilterSelect
            label={pick({ tr: "Sıralama", en: "Sort by" })}
            value={sort}
            onChange={changeFilter(setSort)}
          >
            {[
              ["name", "Ad", "Name"],
              ["oldest", "En eski güncelleme", "Oldest update"],
              ["newest", "En yeni güncelleme", "Newest update"],
              ["records", "Kayıt sayısı", "Record count"],
            ].map(([v, tr, en]) => (
              <option key={v} value={v}>
                {pick({ tr, en })}
              </option>
            ))}
          </FilterSelect>
        </div>
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
        <span aria-live="polite">
          {filtered.length} / {items.length}{" "}
          {pick({ tr: "kaynak", en: "sources" })}
        </span>
        <div className="flex gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setQuery("");
              setStatus("all");
              setKind("all");
              setLanguage("all");
              setPage(1);
            }}
          >
            {pick({ tr: "Filtreleri temizle", en: "Clear filters" })}
          </Button>
          {canWrite && (
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                onSelection(
                  new Set(
                    filtered
                      .filter(hasDraft)
                      .slice(0, 100)
                      .map((s) => s.id),
                  ),
                )
              }
            >
              {pick({
                tr: "Taslakları seç (en fazla 100)",
                en: "Select drafts (up to 100)",
              })}
            </Button>
          )}
        </div>
      </div>
      {bulk}
      {visible.length ? (
        <>
          <div className="hidden overflow-x-auto rounded-xl border bg-card md:block">
            <table className="w-full text-sm">
              <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
                <tr>
                  {canWrite && (
                    <th className="p-4">
                      <input
                        type="checkbox"
                        aria-label={pick({
                          tr: "Bu sayfadaki kaynakları seç",
                          en: "Select sources on this page",
                        })}
                        checked={checked}
                        ref={(el) => {
                          if (el) el.indeterminate = mixed;
                        }}
                        onChange={() =>
                          toggle(
                            visible.map((s) => s.id),
                            checked,
                          )
                        }
                        className="size-4 accent-primary"
                      />
                    </th>
                  )}
                  <th className="p-4">
                    {pick({ tr: "Kaynak", en: "Source" })}
                  </th>
                  <th className="p-4">
                    {pick({ tr: "Yayın / Sağlık", en: "Publication / Health" })}
                  </th>
                  <th className="p-4">
                    {pick({ tr: "Kayıtlar", en: "Records" })}
                  </th>
                  <th className="p-4">
                    {pick({ tr: "Son başarı", en: "Last success" })}
                  </th>
                  <th className="p-4">
                    <span className="sr-only">
                      {pick({ tr: "İşlemler", en: "Actions" })}
                    </span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {visible.map((s) => (
                  <tr
                    key={s.id}
                    className={`border-t ${selected.has(s.id) ? "bg-primary/5" : "hover:bg-muted/20"}`}
                  >
                    {canWrite && (
                      <td className="p-4">
                        <input
                          type="checkbox"
                          aria-label={pick({
                            tr: `${s.name} seç`,
                            en: `Select ${s.name}`,
                          })}
                          checked={selected.has(s.id)}
                          onChange={() => toggle([s.id], selected.has(s.id))}
                          className="size-4 accent-primary"
                        />
                      </td>
                    )}
                    <td className="p-4">
                      <button
                        className="text-left font-medium text-primary hover:underline"
                        onClick={() => onManage(s)}
                      >
                        {s.name}
                      </button>
                      <p
                        className="mt-1 max-w-64 truncate text-xs text-muted-foreground"
                        title={s.url ?? undefined}
                      >
                        {s.url ?? s.kind}
                      </p>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {knowledgeLabel(s.kind, locale)} ·{" "}
                        {s.language.toUpperCase()}
                      </p>
                    </td>
                    <td className="p-4">
                      <div className="flex flex-wrap gap-1.5">
                        <StatusBadge value={s.status} />
                        <StatusBadge value={sourceHealth(s, now)} />
                      </div>
                      {s.last_error && (
                        <p
                          className="mt-2 max-w-56 truncate text-xs text-destructive"
                          title={s.last_error}
                        >
                          {s.last_error}
                        </p>
                      )}
                    </td>
                    <td className="p-4 tabular-nums">{s.records ?? 0}</td>
                    <td className="whitespace-nowrap p-4 text-xs text-muted-foreground">
                      {formatDate(s.last_success_at, locale)}
                    </td>
                    <td className="p-4">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => onManage(s)}
                      >
                        {pick({ tr: "İncele", en: "Inspect" })}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="space-y-3 md:hidden">
            {visible.map((source) => (
              <article
                key={source.id}
                className="space-y-3 rounded-xl border bg-card p-4"
              >
                <div className="flex items-start justify-between gap-3">
                  <button
                    className="text-left font-semibold text-primary"
                    onClick={() => onManage(source)}
                  >
                    {source.name}
                  </button>
                  {canWrite && (
                    <input
                      type="checkbox"
                      className="size-4 accent-primary"
                      aria-label={pick({
                        tr: `${source.name} seç`,
                        en: `Select ${source.name}`,
                      })}
                      checked={selected.has(source.id)}
                      onChange={() =>
                        toggle([source.id], selected.has(source.id))
                      }
                    />
                  )}
                </div>
                <p className="truncate text-xs text-muted-foreground">
                  {source.url ?? source.kind}
                </p>
                <div className="flex flex-wrap gap-2">
                  <StatusBadge value={source.status} />
                  <StatusBadge value={sourceHealth(source, now)} />
                </div>
                <p className="text-xs text-muted-foreground">
                  {source.records ?? 0} {pick({ tr: "kayıt", en: "records" })} ·{" "}
                  {formatDate(source.last_success_at, locale)}
                </p>
                {source.last_error && (
                  <p className="break-words text-xs text-destructive">
                    {source.last_error}
                  </p>
                )}
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => onManage(source)}
                >
                  {pick({ tr: "İncele", en: "Inspect" })}
                </Button>
              </article>
            ))}
          </div>
        </>
      ) : (
        <EmptyState
          title={pick({ tr: "Kaynak bulunamadı", en: "No sources found" })}
          description={pick({
            tr: "Filtreleri temizleyin veya yeni bir kaynak ekleyin.",
            en: "Clear the filters or add a source to get started.",
          })}
        />
      )}
      <div className="flex items-center justify-between">
        <Button
          variant="outline"
          disabled={currentPage === 1}
          onClick={() => setPage(currentPage - 1)}
        >
          {pick({ tr: "Önceki", en: "Previous" })}
        </Button>
        <span className="text-sm">
          {currentPage} / {pages}
        </span>
        <Button
          variant="outline"
          disabled={currentPage === pages}
          onClick={() => setPage(currentPage + 1)}
        >
          {pick({ tr: "Sonraki", en: "Next" })}
        </Button>
      </div>
    </div>
  );
}

export function ActivityView({
  sourceId,
  canWrite = false,
}: {
  sourceId?: string;
  canWrite?: boolean;
}) {
  const { pick, locale } = useLocale();
  const [status, setStatus] = useState("all");
  const [source, setSource] = useState("");
  const [page, setPage] = useState(0);
  const [sourceFilter, setSourceFilter] = useState("");
  const limit = 50;
  const query = useQuery({
    queryKey: ["admin", "ingestion-jobs", sourceId, page, sourceFilter, status],
    queryFn: () =>
      adminGet<{ items: IngestionJob[] }>(
        `ingestion-jobs?limit=${limit}&offset=${page * limit}&source_name=${encodeURIComponent(sourceFilter)}${status !== "all" ? `&job_status=${status}` : ""}${sourceId ? `&source_id=${sourceId}` : ""}`,
      ),
    refetchInterval: 3000,
  });
  const retry = useMutation({
    mutationFn: (id: string) => adminMutate(`sources/${id}/ingest`, "POST", {}),
    onSuccess: () => {
      toast.success(
        pick({
          tr: "Kaynak yenileme sıraya alındı",
          en: "Source refresh queued",
        }),
      );
      void query.refetch();
    },
  });
  if (query.isLoading) return <Skeleton className="h-52" />;
  if (query.error)
    return (
      <ErrorState error={query.error} retry={() => void query.refetch()} />
    );
  const items = query.data?.items ?? [];
  return (
    <div className="space-y-4">
      <form
        className="grid gap-3 sm:grid-cols-[1fr_1fr_auto]"
        onSubmit={(e) => {
          e.preventDefault();
          setSourceFilter(source);
          setPage(0);
        }}
      >
        <Input
          aria-label={pick({
            tr: "İş kaynağı ara",
            en: "Search activity by source",
          })}
          placeholder={pick({ tr: "Kaynak ara", en: "Search source" })}
          value={source}
          onChange={(e) => setSource(e.target.value)}
        />
        <FilterSelect
          label={pick({ tr: "İş durumu", en: "Job status" })}
          value={status}
          onChange={(v) => {
            setStatus(v);
            setPage(0);
          }}
        >
          <option value="all">
            {pick({ tr: "Tüm durumlar", en: "All statuses" })}
          </option>
          {["queued", "leased", "completed", "failed", "dead"].map((value) => (
            <option key={value} value={value}>
              {statusLabel(value, locale)}
            </option>
          ))}
        </FilterSelect>
        <Button type="submit" variant="outline" className="self-end">
          {pick({ tr: "Filtrele", en: "Apply filter" })}
        </Button>
      </form>
      {retry.error && (
        <ErrorState
          error={retry.error}
          retry={() => retry.variables && retry.mutate(retry.variables)}
        />
      )}
      <p className="text-xs text-muted-foreground">
        {pick({
          tr: `Sayfa ${page + 1} · ${items.length} işlem. Her 3 saniyede güncellenir.`,
          en: `Page ${page + 1} · ${items.length} jobs. Updates every 3 seconds.`,
        })}
      </p>
      {items.length ? (
        items.map((j) => (
          <article
            key={j.id}
            className="space-y-3 rounded-xl border bg-card p-4"
          >
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <h3 className="font-medium">{j.source_name}</h3>
                <p className="text-xs text-muted-foreground">
                  {pick({
                    tr:
                      j.kind === "ingest"
                        ? "Kaynak yenileme"
                        : "Yeniden indeksleme",
                    en: j.kind === "ingest" ? "Source refresh" : "Re-indexing",
                  })}{" "}
                  · {j.phase.replaceAll("_", " ")} ·{" "}
                  {pick({ tr: "Deneme", en: "Attempt" })} {j.attempt}
                </p>
              </div>
              <StatusBadge value={j.status} />
            </div>
            <progress
              className="h-2 w-full accent-primary"
              aria-label={`${j.source_name}: ${j.phase}`}
              max={Math.max(j.total_records, 1)}
              value={
                j.total_records
                  ? Math.min(j.processed_records, j.total_records)
                  : j.status === "completed"
                    ? 1
                    : undefined
              }
            />
            <p className="text-xs text-muted-foreground">
              {j.processed_records} / {j.total_records}{" "}
              {pick({ tr: "kayıt", en: "records" })} ·{" "}
              {formatDate(j.created_at, locale)}
              {j.completed_at && ` → ${formatDate(j.completed_at, locale)}`}
            </p>
            {j.error_detail && (
              <p className="break-words rounded-lg bg-destructive/5 p-3 text-sm text-destructive">
                {j.error_code}: {j.error_detail}
              </p>
            )}
            {canWrite && ["failed", "dead"].includes(j.status) && (
              <Button
                variant="outline"
                size="sm"
                disabled={retry.isPending}
                onClick={() => retry.mutate(j.source_id)}
              >
                {pick({ tr: "Kaynağı yeniden al", en: "Refresh source again" })}
              </Button>
            )}
          </article>
        ))
      ) : (
        <EmptyState
          title={pick({ tr: "İşlem bulunamadı", en: "No activity found" })}
        />
      )}
      <div className="flex justify-between">
        <Button
          variant="outline"
          disabled={page === 0}
          onClick={() => setPage((p) => p - 1)}
        >
          {pick({ tr: "Daha yeni", en: "Newer" })}
        </Button>
        <Button
          variant="outline"
          disabled={items.length < limit}
          onClick={() => setPage((p) => p + 1)}
        >
          {pick({ tr: "Daha eski", en: "Older" })}
        </Button>
      </div>
    </div>
  );
}

type Preview = {
  count: number;
  items: {
    external_id: string;
    type: string;
    title: string;
    summary: string | null;
    url: string | null;
    starts_at: string | null;
    ends_at: string | null;
    audience: Record<string, unknown>;
  }[];
};

export function SourceDetail({
  source,
  canWrite,
  onClose,
  onDone,
}: {
  source: KnowledgeSource;
  canWrite: boolean;
  onClose: () => void;
  onDone: () => void;
}) {
  const { pick } = useLocale();
  const query = useQuery({
    queryKey: ["admin", "source", source.id],
    queryFn: () => adminGet<KnowledgeSourceDetail>(`sources/${source.id}`),
  });
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent className="flex max-h-[90dvh] flex-col overflow-hidden sm:max-w-4xl">
        <DialogHeader className="pr-8">
          <DialogTitle>{source.name}</DialogTitle>
          <DialogDescription className="break-all">
            {source.url ?? source.kind}
          </DialogDescription>
        </DialogHeader>
        <div className="min-h-0 overflow-y-auto">
          {query.isLoading ? (
            <Skeleton className="h-64" />
          ) : query.error ? (
            <ErrorState
              error={query.error}
              retry={() => void query.refetch()}
            />
          ) : query.data ? (
            <SourceDetailBody
              source={query.data}
              canWrite={canWrite}
              onDone={() => {
                onDone();
                void query.refetch();
              }}
            />
          ) : (
            <EmptyState
              title={pick({ tr: "Kaynak bulunamadı", en: "Source not found" })}
            />
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function SourceDetailBody({
  source,
  canWrite,
  onDone,
}: {
  source: KnowledgeSourceDetail;
  canWrite: boolean;
  onDone: () => void;
}) {
  const { pick, locale } = useLocale();
  const [view, setView] = useState("overview");
  const [revisionId, setRevisionId] = useState(
    source.revision_history[0]?.id ?? "",
  );
  const revision =
    source.revision_history.find((r) => r.id === revisionId) ??
    source.revision_history[0];
  const [previewed, setPreviewed] = useState("");
  const [confirmRollback, setConfirmRollback] = useState(false);
  const preview = useMutation({
    mutationFn: (r: SourceRevision) =>
      adminMutate<Preview>(
        `sources/${source.id}/revisions/${r.id}/preview`,
        "POST",
        {},
      ),
    onSuccess: (_, r) => setPreviewed(r.id),
  });
  const action = useMutation({
    mutationFn: (path: string) =>
      adminMutate(`sources/${source.id}/${path}`, "POST", {}),
    onSuccess: () => {
      toast.success(
        pick({
          tr: "İşlem tamamlandı; yenileme sıraya alındı",
          en: "Action completed; refresh queued",
        }),
      );
      setConfirmRollback(false);
      onDone();
    },
  });
  const busy = action.isPending || preview.isPending;
  return (
    <div className="space-y-4">
      <SectionNav
        value={view}
        onChange={setView}
        items={[
          ["overview", "Genel bakış", "Overview"],
          ["preview", "İçerik önizleme", "Content preview"],
          ["configuration", "Yapılandırma", "Configuration"],
          ["history", "Geçmiş", "History"],
          ["activity", "İşlemler", "Activity"],
        ].map(([id, tr, en]) => ({ id, label: pick({ tr, en }) }))}
      />
      {view !== "activity" && revision && (
        <FilterSelect
          label={pick({
            tr: "İncelenen revizyon",
            en: "Revision being reviewed",
          })}
          value={revision.id}
          onChange={(v) => {
            setRevisionId(v);
            preview.reset();
            setPreviewed("");
            setConfirmRollback(false);
          }}
        >
          {source.revision_history.map((r) => (
            <option key={r.id} value={r.id}>
              #{r.revision}{" "}
              {r.id === source.active_revision_id
                ? pick({ tr: "· yayında", en: "· live" })
                : ""}{" "}
              · {formatDate(r.created_at, locale)}
            </option>
          ))}
        </FilterSelect>
      )}
      {action.error && (
        <ErrorState
          error={action.error}
          retry={() => action.variables && action.mutate(action.variables)}
        />
      )}
      {view === "overview" && (
        <>
          <div className="flex flex-wrap gap-2">
            <StatusBadge value={source.status} />
            <StatusBadge value={sourceHealth(source, new Date().getTime())} />
            {!canWrite && (
              <span className="text-xs text-muted-foreground">
                {pick({ tr: "Salt okunur erişim", en: "Read-only access" })}
              </span>
            )}
          </div>
          <dl className="grid gap-4 rounded-xl border p-4 sm:grid-cols-2">
            <div>
              <dt className="text-xs text-muted-foreground">
                {pick({ tr: "Son başarı", en: "Last successful refresh" })}
              </dt>
              <dd>{formatDate(source.last_success_at, locale)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">
                {pick({ tr: "Yenileme aralığı", en: "Refresh interval" })}
              </dt>
              <dd>
                {source.schedule_seconds / 60}{" "}
                {pick({ tr: "dakika", en: "minutes" })}
              </dd>
            </div>
          </dl>
          {source.last_error && (
            <p className="break-words rounded-lg bg-destructive/5 p-3 text-destructive">
              {source.last_error}
            </p>
          )}
          <p className="text-sm text-muted-foreground">
            {pick({
              tr: "Yapılandır → İçeriği önizle → Yayınla. Önceki revizyonlar Geçmiş bölümünde korunur.",
              en: "Configure → Preview content → Publish. Previous revisions remain available in History.",
            })}
          </p>
          {canWrite && (
            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                disabled={!source.active_revision_id || busy}
                onClick={() => action.mutate("ingest")}
              >
                {pick({ tr: "Şimdi yenile", en: "Refresh now" })}
              </Button>
              <Button onClick={() => setView("preview")}>
                {pick({ tr: "İçeriği incele", en: "Review content" })}
              </Button>
            </div>
          )}
        </>
      )}
      {view === "preview" &&
        (revision ? (
          <div className="space-y-4">
            <div className="flex gap-2">
              <StatusBadge
                value={revision.validation.ok ? "success" : "failed"}
              />
            </div>
            {[
              ...(revision.validation.errors ?? []),
              ...(revision.validation.warnings ?? []),
            ].map((text, i) => (
              <p key={i} className="rounded-lg border p-3 text-sm">
                {text}
              </p>
            ))}
            {canWrite && (
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => preview.mutate(revision)}
              >
                {preview.isPending
                  ? pick({ tr: "Önizleme yükleniyor…", en: "Loading preview…" })
                  : pick({ tr: "Önizlemeyi yükle", en: "Load preview" })}
              </Button>
            )}
            {!canWrite && (
              <p>
                {pick({
                  tr: "Canlı önizleme için yazma izni gerekir.",
                  en: "Write permission is required to run a live preview.",
                })}
              </p>
            )}
            {preview.error && (
              <ErrorState
                error={preview.error}
                retry={() => preview.mutate(revision)}
              />
            )}
            {preview.data && previewed === revision.id && (
              <>
                <p role="status" className="text-sm">
                  {pick({
                    tr: `${preview.data.count} kayıt bulundu; ilk ${preview.data.items.length} gösteriliyor.`,
                    en: `${preview.data.count} records found; showing the first ${preview.data.items.length}.`,
                  })}
                </p>
                {preview.data.items.map((item, i) => (
                  <article
                    key={`${item.external_id}-${i}`}
                    className="space-y-2 rounded-xl border p-4"
                  >
                    <h3 className="font-semibold">{item.title}</h3>
                    <p className="text-xs text-muted-foreground">
                      {item.type} · {formatDate(item.starts_at, locale)} —{" "}
                      {formatDate(item.ends_at, locale)}
                    </p>
                    <p className="whitespace-pre-wrap text-sm">
                      {item.summary}
                    </p>
                    {item.url && (
                      <a
                        href={item.url}
                        target="_blank"
                        rel="noreferrer"
                        className="break-all text-sm text-primary underline"
                      >
                        {item.url}
                      </a>
                    )}
                    <details className="text-xs">
                      <summary>
                        {pick({ tr: "Hedef kitle", en: "Audience" })}
                      </summary>
                      <pre className="overflow-auto">
                        {JSON.stringify(item.audience, null, 2)}
                      </pre>
                    </details>
                  </article>
                ))}
              </>
            )}
            {canWrite && (
              <Button
                disabled={
                  busy ||
                  !revision.validation.ok ||
                  previewed !== revision.id ||
                  !preview.data?.count ||
                  source.active_revision_id === revision.id
                }
                onClick={() =>
                  action.mutate(`revisions/${revision.id}/publish`)
                }
              >
                {pick({
                  tr: "İncelenen revizyonu yayınla",
                  en: "Publish reviewed revision",
                })}
              </Button>
            )}
          </div>
        ) : (
          <EmptyState
            title={pick({ tr: "Revizyon bulunamadı", en: "No revision found" })}
          />
        ))}
      {view === "configuration" && revision && (
        <RevisionEditor
          key={revision.id}
          source={source}
          revision={revision}
          canWrite={canWrite}
          onDone={(id) => {
            setRevisionId(id);
            setPreviewed("");
            preview.reset();
            onDone();
            setView("preview");
          }}
        />
      )}
      {view === "history" && (
        <div className="space-y-3">
          {source.revision_history.map((r) => (
            <article
              key={r.id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-xl border p-4"
            >
              <div className="space-y-1">
                <p className="font-medium">
                  #{r.revision} · {formatDate(r.created_at, locale)}
                </p>
                <StatusBadge
                  value={
                    r.id === source.active_revision_id ? "published" : r.status
                  }
                />
                {r.published_at && (
                  <p className="text-xs text-muted-foreground">
                    {pick({ tr: "Yayınlandı", en: "Published" })}:{" "}
                    {formatDate(r.published_at, locale)}
                  </p>
                )}
              </div>
              <Button
                variant="outline"
                onClick={() => {
                  setRevisionId(r.id);
                  setView("preview");
                  preview.reset();
                  setPreviewed("");
                }}
              >
                {pick({ tr: "İncele", en: "Inspect" })}
              </Button>
            </article>
          ))}
          {canWrite &&
            revision &&
            revision.published_at &&
            revision.id !== source.active_revision_id &&
            (confirmRollback ? (
              <div className="space-y-3 rounded-xl border p-4">
                <p>
                  {pick({
                    tr: `Revizyon #${revision.revision} yeniden etkinleştirilecek ve yenileme başlayacak.`,
                    en: `Revision #${revision.revision} will become active and a refresh will be queued.`,
                  })}
                </p>
                <Button
                  disabled={busy}
                  onClick={() =>
                    action.mutate(`revisions/${revision.id}/rollback`)
                  }
                >
                  {pick({
                    tr: "Geri yüklemeyi onayla",
                    en: "Confirm rollback",
                  })}
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => setConfirmRollback(false)}
                >
                  {pick({ tr: "Vazgeç", en: "Cancel" })}
                </Button>
              </div>
            ) : (
              <Button
                variant="outline"
                onClick={() => setConfirmRollback(true)}
              >
                {pick({
                  tr: `#${revision.revision} revizyonuna geri dön`,
                  en: `Roll back to revision #${revision.revision}`,
                })}
              </Button>
            ))}
        </div>
      )}
      {view === "activity" && (
        <ActivityView sourceId={source.id} canWrite={canWrite} />
      )}
    </div>
  );
}

function RevisionEditor({
  source,
  revision,
  canWrite,
  onDone,
}: {
  source: KnowledgeSourceDetail;
  revision: SourceRevision;
  canWrite: boolean;
  onDone: (id: string) => void;
}) {
  const { pick } = useLocale();
  const [config, setConfig] = useState(
    JSON.stringify(revision.config, null, 2),
  );
  let parsed: Record<string, unknown> | null = null;
  try {
    const v = JSON.parse(config);
    if (v && typeof v === "object" && !Array.isArray(v)) parsed = v;
  } catch {}
  const save = useMutation({
    mutationFn: () =>
      adminMutate<{ id: string }>(`sources/${source.id}/revisions`, "POST", {
        config: parsed,
      }),
    onSuccess: (d) => onDone(d.id),
  });
  const operating = useMutation({
    mutationFn: () =>
      adminMutate("sources/bulk", "PUT", {
        source_ids: [source.id],
        changes: { enabled: !source.enabled },
      }),
    onSuccess: () => {
      toast.success(
        pick({
          tr: "Çalışma durumu güncellendi",
          en: "Operating state updated",
        }),
      );
      onDone(revision.id);
    },
  });
  return (
    <div className="space-y-3">
      <label htmlFor="revision-config" className="text-sm font-medium">
        {pick({
          tr: "Çıkarma ayarları (JSON)",
          en: "Extraction configuration (JSON)",
        })}
      </label>
      <Textarea
        id="revision-config"
        value={config}
        readOnly={!canWrite}
        onChange={(e) => setConfig(e.target.value)}
        className="min-h-64 font-mono text-xs"
        aria-invalid={!parsed}
      />
      {!parsed && (
        <p role="alert" className="text-sm text-destructive">
          {pick({
            tr: "Geçerli bir JSON nesnesi girin.",
            en: "Enter a valid JSON object.",
          })}
        </p>
      )}
      <p className="text-xs text-muted-foreground">
        {pick({
          tr: "Kaydetmek yeni bir taslak oluşturur. Yayındaki revizyon önizleme ve yayınlama tamamlanana kadar değişmez.",
          en: "Saving creates a new draft. The live revision stays active until you preview and publish the change.",
        })}
      </p>
      {save.error && (
        <ErrorState error={save.error} retry={() => save.mutate()} />
      )}{" "}
      {operating.error && (
        <ErrorState error={operating.error} retry={() => operating.mutate()} />
      )}
      {canWrite && (
        <div className="flex flex-wrap gap-2">
          <Button
            disabled={
              !parsed ||
              save.isPending ||
              config === JSON.stringify(revision.config, null, 2)
            }
            onClick={() => save.mutate()}
          >
            {pick({
              tr: "Taslağı kaydet ve önizle",
              en: "Save draft and preview",
            })}
          </Button>
          <Button
            variant="outline"
            disabled={operating.isPending}
            onClick={() => operating.mutate()}
          >
            {source.enabled
              ? pick({ tr: "Kaynağı devre dışı bırak", en: "Disable source" })
              : pick({ tr: "Kaynağı etkinleştir", en: "Enable source" })}
          </Button>
        </div>
      )}
    </div>
  );
}

export function GroupsView({ canWrite }: { canWrite: boolean }) {
  const { pick, locale } = useLocale();
  const [search, setSearch] = useState("");
  const [editing, setEditing] = useState<CourseGroup | null>(null);
  const query = useQuery({
    queryKey: ["admin", "course-groups"],
    queryFn: () => adminGet<{ items: CourseGroup[] }>("course-groups"),
  });
  if (query.isLoading) return <Skeleton className="h-52" />;
  if (query.error)
    return (
      <ErrorState error={query.error} retry={() => void query.refetch()} />
    );
  const items = (query.data?.items ?? []).filter((g) =>
    `${g.course_code} ${g.section ?? ""}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  return (
    <div className="space-y-4">
      <Input
        aria-label={pick({ tr: "Ders grubu ara", en: "Search course groups" })}
        placeholder={pick({
          tr: "Ders kodu veya şube ara",
          en: "Search course code or section",
        })}
        value={search}
        onChange={(e) => setSearch(e.target.value)}
      />
      <div className="grid gap-3 md:grid-cols-2">
        {items.map((g) => (
          <div key={g.id} className="space-y-3 rounded-xl border bg-card p-4">
            <div className="flex items-center justify-between gap-2">
              <h3 className="font-semibold">
                {g.course_code} {g.section && `· ${g.section}`}
              </h3>
              <StatusBadge
                value={
                  !g.active
                    ? "inactive"
                    : g.valid_until && new Date(g.valid_until) < new Date()
                      ? "expired"
                      : "active"
                }
              />
            </div>
            <p className="text-xs text-muted-foreground">
              {g.valid_until
                ? `${pick({ tr: "Son geçerlilik", en: "Expires" })}: ${formatDate(g.valid_until, locale)}`
                : pick({ tr: "Süre sonu yok", en: "No expiry" })}
            </p>
            {canWrite && (
              <Button variant="outline" size="sm" onClick={() => setEditing(g)}>
                {pick({ tr: "Düzenle / Yenile", en: "Edit / Renew" })}
              </Button>
            )}
          </div>
        ))}
      </div>
      {!items.length && (
        <EmptyState
          title={pick({
            tr: "Ders grubu bulunamadı",
            en: "No course groups found",
          })}
        />
      )}{" "}
      {editing && (
        <GroupEditor
          key={editing.id}
          group={editing}
          onClose={() => setEditing(null)}
          onDone={() => {
            setEditing(null);
            void query.refetch();
          }}
        />
      )}
    </div>
  );
}
function GroupEditor({
  group,
  onClose,
  onDone,
}: {
  group: CourseGroup;
  onClose: () => void;
  onDone: () => void;
}) {
  const { pick } = useLocale();
  const [course, setCourse] = useState(group.course_code);
  const [section, setSection] = useState(group.section ?? "");
  const [url, setUrl] = useState("");
  const [active, setActive] = useState(group.active);
  const date = group.valid_until ? new Date(group.valid_until) : null;
  const local = date
    ? new Date(date.getTime() - date.getTimezoneOffset() * 60000)
        .toISOString()
        .slice(0, 16)
    : "";
  const [expiry, setExpiry] = useState(local);
  const mutation = useMutation({
    mutationFn: () =>
      adminMutate(`course-groups/${group.id}`, "PUT", {
        course_code: course,
        section: section || null,
        invite_url: url || null,
        active,
        valid_until: expiry ? new Date(expiry).toISOString() : null,
      }),
    onSuccess: onDone,
  });
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {pick({ tr: "Ders grubunu düzenle", en: "Edit course group" })}
          </DialogTitle>
          <DialogDescription>
            {pick({
              tr: "Mevcut davet bağlantısını korumak için bağlantı alanını boş bırakın.",
              en: "Leave the invite link blank to keep the existing encrypted link.",
            })}
          </DialogDescription>
        </DialogHeader>
        <form
          className="space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            mutation.mutate();
          }}
        >
          <label className="block space-y-1">
            <span>{pick({ tr: "Ders kodu", en: "Course code" })}</span>
            <Input
              value={course}
              required
              minLength={2}
              maxLength={32}
              onChange={(e) => setCourse(e.target.value)}
            />
          </label>
          <label className="block space-y-1">
            <span>{pick({ tr: "Şube", en: "Section" })}</span>
            <Input
              value={section}
              maxLength={16}
              onChange={(e) => setSection(e.target.value)}
            />
          </label>
          <label className="block space-y-1">
            <span>
              {pick({ tr: "Yeni davet bağlantısı", en: "New invite link" })}
            </span>
            <Input
              type="url"
              value={url}
              minLength={10}
              onChange={(e) => setUrl(e.target.value)}
            />
          </label>
          <label className="block space-y-1">
            <span>
              {pick({
                tr: "Son geçerlilik (yerel saat)",
                en: "Expires at (local time)",
              })}
            </span>
            <Input
              type="datetime-local"
              value={expiry}
              onChange={(e) => setExpiry(e.target.value)}
            />
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={active}
              onChange={(e) => setActive(e.target.checked)}
            />
            {pick({ tr: "Etkin", en: "Active" })}
          </label>
          {mutation.error && (
            <p role="alert" className="text-destructive">
              {mutation.error.message}
            </p>
          )}
          <div className="flex gap-2">
            <Button disabled={mutation.isPending} type="submit">
              {pick({ tr: "Kaydet", en: "Save changes" })}
            </Button>
            <Button type="button" variant="ghost" onClick={onClose}>
              {pick({ tr: "Vazgeç", en: "Cancel" })}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
