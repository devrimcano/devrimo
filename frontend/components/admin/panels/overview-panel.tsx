"use client";

import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  BotIcon,
  Building2Icon,
  CheckCircle2Icon,
  ClipboardCheckIcon,
  CoinsIcon,
  CpuIcon,
  RefreshCwIcon,
  SparklesIcon,
  UsersIcon,
} from "lucide-react";
import { useLocale } from "@/components/locale-provider";
import { EmptyState, ErrorState, LoadingCards, PanelHeader, RatioBar, StatusBadge, formatDate } from "@/components/admin/admin-shared";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { adminGet } from "@/lib/admin/client";
import type { Overview } from "@/lib/admin/types";
import { cn } from "@/lib/utils";

export function OverviewPanel({ title, description }: { title: string; description: string }) {
  const { locale, pick } = useLocale();
  const query = useQuery({
    queryKey: ["admin", "overview"],
    queryFn: () => adminGet<Overview>("overview"),
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
  });

  const actions = query.data ? (
    <>
      <span className="text-xs text-muted-foreground">
        {pick({ tr: "Güncellendi", en: "Updated" })} {formatDate(query.data.fresh_at, locale)}
      </span>
      <Button variant="outline" size="sm" onClick={() => void query.refetch()} disabled={query.isFetching}>
        <RefreshCwIcon className={cn(query.isFetching && "animate-spin")} />
        {pick({ tr: "Yenile", en: "Refresh" })}
      </Button>
    </>
  ) : undefined;

  return (
    <>
      <PanelHeader title={title} description={description} actions={actions} />
      {query.isLoading ? <LoadingCards /> : query.error ? <ErrorState error={query.error} retry={() => void query.refetch()} /> : <OverviewContent data={query.data!} />}
    </>
  );
}

function OverviewContent({ data }: { data: Overview }) {
  const { locale, pick } = useLocale();
  const number = new Intl.NumberFormat(locale === "tr" ? "tr-TR" : "en-US");
  const compact = new Intl.NumberFormat(locale === "tr" ? "tr-TR" : "en-US", { notation: "compact", maximumFractionDigits: 1 });
  const usd = new Intl.NumberFormat(locale === "tr" ? "tr-TR" : "en-US", { style: "currency", currency: "USD", maximumFractionDigits: 4 });
  const metrics = [
    { label: pick({ tr: "Toplam hesap", en: "Total accounts" }), value: data.users, icon: UsersIcon, hint: pick({ tr: "Dizindeki tüm etkin kayıtlar", en: "All current directory records" }) },
    { label: pick({ tr: "Aktif hesap", en: "Active accounts" }), value: data.active_users, icon: CheckCircle2Icon, hint: pick({ tr: `${data.users ? Math.round((data.active_users / data.users) * 100) : 0}% etkin`, en: `${data.users ? Math.round((data.active_users / data.users) * 100) : 0}% active` }) },
    { label: pick({ tr: "Kurulumu tamamlayan", en: "Onboarding complete" }), value: data.onboarding_completed, icon: ClipboardCheckIcon, hint: pick({ tr: `${data.users ? Math.round((data.onboarding_completed / data.users) * 100) : 0}% tamamlandı`, en: `${data.users ? Math.round((data.onboarding_completed / data.users) * 100) : 0}% complete` }) },
    { label: pick({ tr: "METU bağlantısı", en: "METU connected" }), value: data.campus_connected, icon: Building2Icon, hint: pick({ tr: `${data.users ? Math.round((data.campus_connected / data.users) * 100) : 0}% bağlı`, en: `${data.users ? Math.round((data.campus_connected / data.users) * 100) : 0}% connected` }) },
  ];

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {metrics.map(({ label, value, icon: Icon, hint }) => (
          <Card key={label} className="surface-raised border-0 ring-1 ring-foreground/8">
            <CardContent>
              <div className="flex items-start justify-between gap-4">
                <div>
                  <p className="text-sm font-medium text-muted-foreground">{label}</p>
                  <p className="mt-2 text-3xl font-semibold tracking-tight tabular-nums">{value}</p>
                  <p className="mt-1 text-xs text-muted-foreground">{hint}</p>
                </div>
                <span className="grid size-9 place-items-center rounded-xl border border-primary/10 bg-primary/[0.075] text-primary">
                  <Icon className="size-4" />
                </span>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card className="overflow-hidden border-primary/18 bg-primary/[0.045] ring-1 ring-primary/8">
        <CardHeader className="border-b border-primary/10">
          <div className="flex items-start justify-between gap-4">
            <div>
              <CardTitle className="flex items-center gap-2"><CpuIcon className="size-4 text-primary" />{pick({ tr: "Token kullanımı", en: "Token usage" })}</CardTitle>
              <CardDescription>{pick({ tr: "Agno çalışma metriklerinden hesaplanır; mesaj veya öğrenci içeriği okunmaz.", en: "Calculated from Agno run metrics; no messages or student content are read." })}</CardDescription>
            </div>
            <span className="rounded-lg border bg-background/70 px-2.5 py-1 text-xs font-medium tabular-nums">{number.format(data.usage.runs)} {pick({ tr: "çalışma", en: "runs" })}</span>
          </div>
        </CardHeader>
        <CardContent className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <UsageMetric label={pick({ tr: "Toplam token", en: "Total tokens" })} value={number.format(data.usage.total_tokens)} hint={`${compact.format(data.usage.input_tokens)} ${pick({ tr: "girdi", en: "input" })} · ${compact.format(data.usage.output_tokens)} ${pick({ tr: "çıktı", en: "output" })}`} />
          <UsageMetric label={pick({ tr: "Son 24 saat", en: "Last 24 hours" })} value={number.format(data.usage.last_24h_tokens)} hint={pick({ tr: "Tüm ajan çalışmaları", en: "All agent runs" })} />
          <UsageMetric label={pick({ tr: "Son 7 gün", en: "Last 7 days" })} value={number.format(data.usage.last_7d_tokens)} hint={pick({ tr: "Kayan zaman aralığı", en: "Rolling time window" })} />
          <UsageMetric label={pick({ tr: "Tahmini maliyet", en: "Estimated cost" })} value={usd.format(data.usage.estimated_cost_usd)} hint={pick({ tr: "Paneldeki güncel token fiyatlarıyla", en: "Using current token prices" })} icon={<CoinsIcon className="size-4 text-primary" />} />
        </CardContent>
        <div className="grid gap-px border-t border-primary/10 bg-border/70 sm:grid-cols-3">
          <UsageBreakdown label={pick({ tr: "Ana model", en: "Primary model" })} value={number.format(data.usage.primary_model_tokens)} />
          <UsageBreakdown label={pick({ tr: "Sıkıştırma ek yükü", en: "Compression overhead" })} value={number.format(data.usage.compression_tokens)} />
          <UsageBreakdown label={pick({ tr: "Öğrenme ek yükü", en: "Learning overhead" })} value={number.format(data.usage.learning_tokens)} />
        </div>
      </Card>

      <div className="grid gap-4 xl:grid-cols-[1.4fr_1fr]">
        <Card className="surface-raised border-0 ring-1 ring-foreground/8">
          <CardHeader className="border-b">
            <CardTitle>{pick({ tr: "Operasyonel dikkat", en: "Operational attention" })}</CardTitle>
            <CardDescription>{pick({ tr: "Askıya alınmış hesaplar ve hata durumundaki ajanlar.", en: "Suspended accounts and agents currently in an error state." })}</CardDescription>
          </CardHeader>
          <CardContent className="pt-0">
            {data.attention.length ? (
              <div className="divide-y">
                {data.attention.map((item) => (
                  <div key={item.user_id} className="flex flex-col gap-3 py-3.5 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0">
                      <p className="truncate font-medium">{item.email ?? item.user_id}</p>
                      <p className="mt-0.5 truncate font-mono text-[11px] text-muted-foreground">{item.user_id}</p>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <StatusBadge value={item.account_status} />
                      <StatusBadge value={item.agent_status} />
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState title={pick({ tr: "Dikkat gerektiren öğe yok", en: "Nothing needs attention" })} description={pick({ tr: "Tüm izlenen hesaplar ve ajanlar normal görünüyor.", en: "All monitored accounts and agents look normal." })} icon={<SparklesIcon className="size-4" />} />
            )}
          </CardContent>
        </Card>

        <Card className="surface-raised border-0 ring-1 ring-foreground/8">
          <CardHeader className="border-b">
            <CardTitle>{pick({ tr: "Kullanılabilirlik", en: "Availability" })}</CardTitle>
            <CardDescription>{pick({ tr: "Hesap hazırlığı ve bu brokerdaki yerleşik çalışma zamanları.", en: "Account readiness and runtimes resident on this broker." })}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <RatioBar value={data.onboarding_completed} total={data.users} label={pick({ tr: "Kurulum tamamlandı", en: "Onboarding complete" })} />
            <RatioBar value={data.campus_connected} total={data.users} label={pick({ tr: "METU bağlantısı", en: "METU connection" })} />
            <div className="space-y-2 border-t pt-4">
              {Object.entries(data.agents).map(([key, value]) => (
                <div key={key} className="flex items-center justify-between gap-3">
                  <StatusBadge value={key} />
                  <span className="font-semibold tabular-nums">{value}</span>
                </div>
              ))}
              <div className="flex items-center justify-between border-t pt-3">
                <span className="flex items-center gap-2 text-sm"><BotIcon className="size-4 text-primary" />{pick({ tr: "Şu anda yerleşik", en: "Resident now" })}</span>
                <span className="font-semibold tabular-nums">{data.resident_agents}</span>
              </div>
            </div>
            <p className="text-xs text-muted-foreground">{pick({ tr: "Son veri", en: "Fresh at" })}: {formatDate(data.fresh_at, locale)}</p>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function UsageMetric({ label, value, hint, icon }: { label: string; value: string; hint: string; icon?: ReactNode }) {
  return <div className="rounded-xl border bg-background/65 p-4"><div className="flex items-center justify-between gap-2"><p className="text-xs font-medium text-muted-foreground">{label}</p>{icon}</div><p className="mt-2 text-2xl font-semibold tracking-tight tabular-nums">{value}</p><p className="mt-1 text-xs text-muted-foreground">{hint}</p></div>;
}

function UsageBreakdown({ label, value }: { label: string; value: string }) {
  return <div className="flex items-center justify-between gap-3 bg-background/72 px-4 py-3 text-xs"><span className="text-muted-foreground">{label}</span><span className="font-semibold tabular-nums">{value}</span></div>;
}
