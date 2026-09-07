"use client";

import { useEffect, useMemo, useState, type ComponentType } from "react";
import { AnimatePresence, MotionConfig, motion, useReducedMotion } from "motion/react";
import { useSearchParams } from "next/navigation";
import {
  ActivityIcon,
  BotIcon,
  Building2Icon,
  ChevronRightIcon,
  ClipboardListIcon,
  DatabaseZapIcon,
  GaugeIcon,
  GraduationCapIcon,
  MenuIcon,
  ShieldCheckIcon,
  SlidersHorizontalIcon,
  UserCogIcon,
  UsersIcon,
  XIcon,
} from "lucide-react";
import { useLocale } from "@/components/locale-provider";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import type { AdminPrincipal } from "@/lib/admin/types";
import type { Copy } from "@/components/admin/admin-shared";
import { OverviewPanel } from "@/components/admin/panels/overview-panel";
import { UsersPanel } from "@/components/admin/panels/users-panel";
import { ResearchersPanel } from "@/components/admin/panels/researchers-panel";
import { KnowledgePanel } from "@/components/admin/panels/knowledge-panel";
import {
  AgentsPanel,
  AuditPanel,
  IntegrationsPanel,
} from "@/components/admin/panels/operations-panels";
import {
  AccessPanel,
  RuntimePanel,
  SystemPanel,
} from "@/components/admin/panels/settings-panels";

export type AdminSection =
  | "overview"
  | "users"
  | "agents"
  | "integrations"
  | "knowledge"
  | "researchers"
  | "audit"
  | "access"
  | "runtime"
  | "system";

type NavItem = {
  id: AdminSection;
  label: Copy;
  description: Copy;
  icon: ComponentType<{ className?: string }>;
  permission: string;
  group: "manage" | "operate" | "configure";
};

const NAV: NavItem[] = [
  { id: "overview", label: { tr: "Genel bakış", en: "Overview" }, description: { tr: "Hesaplar, bağlantılar ve çalışma zamanı sağlığı.", en: "Accounts, connections, and runtime health." }, icon: GaugeIcon, permission: "overview:read", group: "manage" },
  { id: "users", label: { tr: "Kullanıcılar", en: "Users" }, description: { tr: "Hesap desteği, davetler ve yaşam döngüsü.", en: "Account support, invitations, and lifecycle." }, icon: UsersIcon, permission: "users:read", group: "manage" },
  { id: "agents", label: { tr: "Ajanlar", en: "Agents" }, description: { tr: "Yerleşik ajanlar ve korumalı işlemler.", en: "Resident agents and guarded operations." }, icon: BotIcon, permission: "agents:read", group: "operate" },
  { id: "integrations", label: { tr: "Entegrasyonlar", en: "Integrations" }, description: { tr: "METU araçlarının benimsenmesi ve doğrulanması.", en: "METU tool adoption and verification." }, icon: Building2Icon, permission: "integrations:read", group: "operate" },
  { id: "knowledge", label: { tr: "Kampüs bilgisi", en: "Campus knowledge" }, description: { tr: "Kaynakları, içe aktarmayı ve korumalı ders gruplarını yönetin.", en: "Manage sources, ingestion, and protected course groups." }, icon: DatabaseZapIcon, permission: "knowledge:read", group: "operate" },
  { id: "researchers", label: { tr: "Araştırmacılar", en: "Researchers" }, description: { tr: "AVESIS profillerini ve içe aktarma ilerlemesini yönetin.", en: "Manage AVESIS profiles and import progress." }, icon: GraduationCapIcon, permission: "researchers:read", group: "operate" },
  { id: "audit", label: { tr: "Denetim kaydı", en: "Audit log" }, description: { tr: "İçeriksiz yönetici işlemleri ve sonuçları.", en: "Content-free admin operations and results." }, icon: ClipboardListIcon, permission: "audit:read", group: "operate" },
  { id: "access", label: { tr: "Yönetici erişimi", en: "Admin access" }, description: { tr: "Yönetici üyelikleri ve rol atamaları.", en: "Admin memberships and role assignments." }, icon: UserCogIcon, permission: "memberships:manage", group: "configure" },
  { id: "runtime", label: { tr: "Ajan varsayılanları", en: "Agent defaults" }, description: { tr: "Model ve güvenli davranış varsayılanları.", en: "Model and safe-behavior defaults." }, icon: SlidersHorizontalIcon, permission: "runtime:read", group: "configure" },
  { id: "system", label: { tr: "Sistem", en: "System" }, description: { tr: "Broker, veri kaynakları ve çalışma zamanı havuzu.", en: "Broker, data sources, and runtime pool." }, icon: ActivityIcon, permission: "system:read", group: "configure" },
];

const GROUP_LABELS: Record<NavItem["group"], Copy> = {
  manage: { tr: "Yönet", en: "Manage" },
  operate: { tr: "Operasyon", en: "Operations" },
  configure: { tr: "Yapılandır", en: "Configure" },
};

export function AdminDashboard({ principal }: { principal: AdminPrincipal }) {
  const { pick } = useLocale();
  const searchParams = useSearchParams();
  const reduceMotion = useReducedMotion();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const available = useMemo(() => NAV.filter((item) => principal.permissions.includes(item.permission)), [principal.permissions]);
  const requested = searchParams.get("section") as AdminSection | null;
  const active = available.find((item) => item.id === requested) ?? available[0];

  useEffect(() => {
    if (!active || requested === active.id) return;
    const params = new URLSearchParams(searchParams.toString());
    params.set("section", active.id);
    window.history.replaceState(null, "", `?${params.toString()}`);
  }, [active, requested, searchParams]);

  if (!active) return null;

  function selectSection(id: AdminSection) {
    const params = new URLSearchParams(searchParams.toString());
    params.set("section", id);
    window.history.pushState(null, "", `?${params.toString()}`);
    setMobileNavOpen(false);
  }

  return (
    <MotionConfig reducedMotion="user">
      <main className="campus-grid flex min-h-0 flex-1 overflow-hidden bg-background">
        <aside className="hidden w-64 shrink-0 border-r border-sidebar-border bg-sidebar/95 lg:flex lg:flex-col">
          <AdminSidebar available={available} activeId={active.id} principal={principal} onSelect={selectSection} />
        </aside>

        <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
          <div className="z-20 flex h-13 shrink-0 items-center gap-3 border-b bg-background/88 px-4 backdrop-blur-xl lg:px-6">
            <Button variant="outline" size="icon" className="lg:hidden" onClick={() => setMobileNavOpen(true)} aria-label={pick({ tr: "Yönetim menüsünü aç", en: "Open admin menu" })}>
              <MenuIcon />
            </Button>
            <div className="flex min-w-0 items-center gap-2 text-sm">
              <span className="hidden text-muted-foreground sm:inline">{pick({ tr: "Yönetim", en: "Admin" })}</span>
              <ChevronRightIcon className="hidden size-3 text-muted-foreground sm:block" />
              <span className="truncate font-medium">{pick(active.label)}</span>
            </div>
            <div className="ml-auto flex items-center gap-2">
              <span className="hidden text-xs text-muted-foreground md:inline">{pick({ tr: "Gizlilik güvenli görünüm", en: "Privacy-safe view" })}</span>
              <Badge variant="outline" className="max-w-44 truncate bg-card/70 capitalize">
                {principal.bootstrap ? "bootstrap · " : ""}{principal.role.replaceAll("_", " ")}
              </Badge>
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto">
            <div className="mx-auto w-full max-w-[1480px] p-4 sm:p-5 lg:p-7">
              <AnimatePresence mode="wait" initial={false}>
                <motion.div
                  key={active.id}
                  initial={reduceMotion ? false : { opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={reduceMotion ? undefined : { opacity: 0, y: -4 }}
                  transition={{ duration: reduceMotion ? 0 : 0.18, ease: "easeOut" }}
                >
                  <AdminPanel section={active.id} principal={principal} title={pick(active.label)} description={pick(active.description)} />
                </motion.div>
              </AnimatePresence>
            </div>
          </div>
        </div>

        <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
          <SheetContent side="left" className="w-[19rem] max-w-[88vw] gap-0 bg-sidebar p-0" showCloseButton={false}>
            <SheetHeader className="sr-only">
              <SheetTitle>{pick({ tr: "Yönetim menüsü", en: "Admin menu" })}</SheetTitle>
              <SheetDescription>{pick({ tr: "Yönetim bölümleri", en: "Admin sections" })}</SheetDescription>
            </SheetHeader>
            <Button variant="ghost" size="icon-sm" className="absolute right-3 top-3 z-10" onClick={() => setMobileNavOpen(false)} aria-label={pick({ tr: "Menüyü kapat", en: "Close menu" })}>
              <XIcon />
            </Button>
            <AdminSidebar available={available} activeId={active.id} principal={principal} onSelect={selectSection} />
          </SheetContent>
        </Sheet>
      </main>
    </MotionConfig>
  );
}

function AdminSidebar({ available, activeId, principal, onSelect }: { available: NavItem[]; activeId: AdminSection; principal: AdminPrincipal; onSelect: (id: AdminSection) => void }) {
  const { pick } = useLocale();
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b border-sidebar-border p-4">
        <div className="flex items-center gap-2.5">
          <span className="grid size-9 place-items-center rounded-xl bg-primary text-primary-foreground shadow-[0_3px_0_color-mix(in_srgb,var(--primary),black_28%)]">
            <ShieldCheckIcon className="size-4" />
          </span>
          <div className="min-w-0">
            <p className="font-semibold tracking-tight">{pick({ tr: "Yönetim merkezi", en: "Admin center" })}</p>
            <p className="truncate text-xs text-muted-foreground">METU · {principal.email ?? principal.user_id}</p>
          </div>
        </div>
      </div>

      <nav className="min-h-0 flex-1 space-y-5 overflow-y-auto p-3" aria-label={pick({ tr: "Yönetim bölümleri", en: "Admin sections" })}>
        {(Object.keys(GROUP_LABELS) as NavItem["group"][]).map((group) => {
          const items = available.filter((item) => item.group === group);
          if (!items.length) return null;
          return (
            <div key={group}>
              <p className="mb-1.5 px-3 text-[10px] font-semibold tracking-[0.14em] text-muted-foreground uppercase">{pick(GROUP_LABELS[group])}</p>
              <div className="space-y-1">
                {items.map((item) => <AdminNavButton key={item.id} item={item} active={activeId === item.id} onClick={() => onSelect(item.id)} />)}
              </div>
            </div>
          );
        })}
      </nav>

      <div className="m-3 rounded-xl border border-primary/15 bg-primary/[0.045] p-3 text-xs leading-5 text-muted-foreground">
        <p className="font-medium text-foreground">{pick({ tr: "Gizlilik sınırı", en: "Privacy boundary" })}</p>
        <p className="mt-1">{pick({ tr: "Mesajlar, ders kayıtları, e-posta içerikleri ve kimlik bilgileri burada gösterilmez.", en: "Messages, academic records, email contents, and credentials are never shown here." })}</p>
      </div>
    </div>
  );
}

function AdminNavButton({ item, active, onClick }: { item: NavItem; active: boolean; onClick: () => void }) {
  const { pick } = useLocale();
  const Icon = item.icon;
  return (
    <button type="button" onClick={onClick} aria-current={active ? "page" : undefined} className={cn("relative flex min-h-10 w-full items-center gap-2.5 overflow-hidden rounded-lg px-3 text-left text-sm outline-none transition-colors focus-visible:ring-2 focus-visible:ring-sidebar-ring", active ? "text-primary" : "text-sidebar-foreground hover:bg-sidebar-accent/65")}>
      {active ? <motion.span layoutId="admin-active-nav" className="absolute inset-0 rounded-lg border border-primary/15 bg-sidebar-accent" transition={{ type: "spring", stiffness: 450, damping: 38 }} /> : null}
      <Icon className="relative z-10 size-4" />
      <span className="relative z-10 font-medium">{pick(item.label)}</span>
      {active ? <ChevronRightIcon className="relative z-10 ml-auto size-3.5" /> : null}
    </button>
  );
}

function AdminPanel({ section, principal, title, description }: { section: AdminSection; principal: AdminPrincipal; title: string; description: string }) {
  switch (section) {
    case "overview": return <OverviewPanel title={title} description={description} />;
    case "users": return <UsersPanel principal={principal} title={title} description={description} />;
    case "agents": return <AgentsPanel principal={principal} title={title} description={description} />;
    case "integrations": return <IntegrationsPanel title={title} description={description} />;
    case "knowledge": return <KnowledgePanel principal={principal} title={title} description={description} />;
    case "researchers": return <ResearchersPanel principal={principal} title={title} description={description} />;
    case "audit": return <AuditPanel principal={principal} title={title} description={description} />;
    case "access": return <AccessPanel principal={principal} title={title} description={description} />;
    case "runtime": return <RuntimePanel title={title} description={description} />;
    case "system": return <SystemPanel principal={principal} title={title} description={description} />;
  }
}
