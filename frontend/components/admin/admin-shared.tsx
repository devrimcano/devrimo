"use client";

import type { ReactNode } from "react";
import {
  AlertCircleIcon,
  CheckCircle2Icon,
  CircleDashedIcon,
  RefreshCwIcon,
  SearchIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { useLocale, type Locale } from "@/components/locale-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";

export type Copy = { tr: string; en: string };

const STATUS_COPY: Record<string, Copy> = {
  leased: { tr: "Çalışıyor", en: "Running" },
  dead: { tr: "Müdahale gerekli", en: "Needs intervention" },
  published: { tr: "Yayında", en: "Published" },
  draft: { tr: "Taslak", en: "Draft" },
  valid: { tr: "Geçerli", en: "Valid" },
  disabled: { tr: "Devre dışı", en: "Disabled" },
  inactive: { tr: "Etkin değil", en: "Inactive" },
  expired: { tr: "Süresi dolmuş", en: "Expired" },
  stale: { tr: "Gecikmiş", en: "Overdue" },
  healthy: { tr: "Güncel", en: "Up to date" },
  not_ingested: { tr: "Henüz alınmadı", en: "Not ingested" },
  queued: { tr: "Sırada", en: "Queued" },
  pending: { tr: "Bekliyor", en: "Pending" },
  completed: { tr: "Tamamlandı", en: "Completed" },
  dead_letter: { tr: "Müdahale gerekli", en: "Needs intervention" },
  retry: { tr: "Yeniden deneniyor", en: "Retrying" },
  active: { tr: "Etkin", en: "Active" },
  running: { tr: "Çalışıyor", en: "Running" },
  success: { tr: "Başarılı", en: "Success" },
  ok: { tr: "Sağlıklı", en: "Healthy" },
  configured: { tr: "Yapılandırıldı", en: "Configured" },
  connected: { tr: "Bağlı", en: "Connected" },
  suspended: { tr: "Askıya alındı", en: "Suspended" },
  error: { tr: "Hata", en: "Error" },
  failed: { tr: "Başarısız", en: "Failed" },
  deletion_pending: { tr: "Silme bekliyor", en: "Deletion pending" },
  stopped: { tr: "Durduruldu", en: "Stopped" },
  provisioning: { tr: "Hazırlanıyor", en: "Provisioning" },
  not_configured: { tr: "Yapılandırılmadı", en: "Not configured" },
  super_admin: { tr: "Süper yönetici", en: "Super admin" },
  campus_admin: { tr: "Kampüs yöneticisi", en: "Campus admin" },
  operator: { tr: "Operatör", en: "Operator" },
};

const BAD_STATUSES = new Set([
  "error",
  "suspended",
  "deletion_pending",
  "failed",
  "dead_letter",
  "dead",
  "stale",
]);
const GOOD_STATUSES = new Set([
  "active",
  "running",
  "leased",
  "success",
  "ok",
  "configured",
  "connected",
  "published",
  "healthy",
  "completed",
]);

export function formatDate(value: string | null | undefined, locale: Locale) {
  return value
    ? new Intl.DateTimeFormat(locale === "tr" ? "tr-TR" : "en-US", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(new Date(value))
    : "—";
}

export function statusLabel(value: string | null | undefined, locale: Locale) {
  if (!value) return "—";
  return STATUS_COPY[value]?.[locale] ?? value.replaceAll("_", " ");
}

export function StatusBadge({
  value,
  className,
}: {
  value: string | null | undefined;
  className?: string;
}) {
  const { locale } = useLocale();
  const normalized = value ?? "";
  const bad = BAD_STATUSES.has(normalized);
  const good = GOOD_STATUSES.has(normalized);
  const Icon = bad
    ? AlertCircleIcon
    : good
      ? CheckCircle2Icon
      : CircleDashedIcon;

  return (
    <Badge
      variant={bad ? "destructive" : good ? "secondary" : "outline"}
      className={cn(
        "gap-1 capitalize",
        good && "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
        className,
      )}
    >
      <Icon data-icon="inline-start" />
      {statusLabel(value, locale)}
    </Badge>
  );
}

export function LoadingCards({ count = 4 }: { count?: number }) {
  return (
    <div
      className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4"
      role="status"
      aria-label="Loading"
    >
      {Array.from({ length: count }).map((_, index) => (
        <div
          key={index}
          className="h-28 animate-pulse rounded-xl border bg-card/60"
        />
      ))}
    </div>
  );
}

export function ErrorState({
  error,
  retry,
}: {
  error: Error;
  retry: () => void;
}) {
  const { pick } = useLocale();
  return (
    <Card className="border-destructive/25 bg-destructive/[0.035]">
      <CardContent className="flex flex-col items-start justify-between gap-4 sm:flex-row sm:items-center">
        <div className="flex gap-3">
          <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-destructive/10 text-destructive">
            <TriangleAlertIcon className="size-4" />
          </span>
          <div>
            <p className="font-medium text-destructive">
              {pick({
                tr: "Bu bölüm yüklenemedi",
                en: "This section could not load",
              })}
            </p>
            <p className="mt-1 break-words text-sm text-muted-foreground">
              {error.message}
            </p>
          </div>
        </div>
        <Button variant="outline" onClick={retry}>
          <RefreshCwIcon />
          {pick({ tr: "Yeniden dene", en: "Retry" })}
        </Button>
      </CardContent>
    </Card>
  );
}

export function EmptyState({
  title,
  description,
  icon,
}: {
  title: string;
  description?: string;
  icon?: ReactNode;
}) {
  return (
    <div className="flex min-h-44 flex-col items-center justify-center px-6 py-10 text-center">
      <span className="mb-3 grid size-10 place-items-center rounded-xl bg-muted text-muted-foreground">
        {icon ?? <CircleDashedIcon className="size-4" />}
      </span>
      <p className="font-medium">{title}</p>
      {description ? (
        <p className="mt-1 max-w-sm text-sm text-muted-foreground">
          {description}
        </p>
      ) : null}
    </div>
  );
}

export function SearchField({
  value,
  onChange,
  placeholder,
  className,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  className?: string;
}) {
  return (
    <div className={cn("relative min-w-0 flex-1", className)}>
      <SearchIcon className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
      <Input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="h-9 bg-card pl-9"
        type="search"
      />
    </div>
  );
}

export function Detail({
  label,
  children,
  className,
}: {
  label: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("rounded-xl border bg-muted/25 p-3", className)}>
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <div className="mt-1.5 min-w-0 font-medium">{children}</div>
    </div>
  );
}

export function RatioBar({
  value,
  total,
  label,
}: {
  value: number;
  total: number;
  label: string;
}) {
  const percent = total > 0 ? Math.round((value / total) * 100) : 0;
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3 text-sm">
        <span className="text-muted-foreground">{label}</span>
        <span className="font-semibold tabular-nums">
          {value}{" "}
          <span className="font-normal text-muted-foreground">
            · {percent}%
          </span>
        </span>
      </div>
      <Progress
        value={percent}
        aria-label={`${label}: ${percent}%`}
        className="gap-0"
      />
    </div>
  );
}

export function PanelHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-5 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold tracking-[-0.025em] sm:text-[1.75rem]">
          {title}
        </h1>
        <p className="mt-1 max-w-2xl text-sm leading-6 text-muted-foreground">
          {description}
        </p>
      </div>
      {actions ? (
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {actions}
        </div>
      ) : null}
    </div>
  );
}
