import Link from "next/link";
import { ArrowRightIcon, CalendarDaysIcon, SettingsIcon } from "lucide-react";

import { SchedulePlanner } from "@/components/schedule/schedule-planner";
import { buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";

// Rollout is deliberately local and default-closed. The planner is unavailable
// until the iterative flow is enabled explicitly for a deployment or development
// session; no remote flag is consulted here and no legacy planner bypasses the
// verified timetable flow.
const iterativePlannerEnabled = process.env.NEXT_PUBLIC_SCHEDULE_PLANNER_ITERATIVE === "true";

export default function SchedulePage() {
  return iterativePlannerEnabled ? <SchedulePlanner /> : <PlannerUnavailable />;
}

function PlannerUnavailable() {
  return (
    <main className="h-full overflow-y-auto bg-[radial-gradient(circle_at_85%_0%,rgb(227_24_55/8%),transparent_32%)] px-4 py-6 sm:px-6 lg:px-8">
      <div className="mx-auto flex min-h-full max-w-2xl items-center justify-center">
        <Card className="w-full border-primary/20 bg-card/95 shadow-sm">
          <CardContent className="space-y-6 p-6 sm:p-8">
            <div className="flex size-12 items-center justify-center rounded-2xl bg-primary/10 text-primary">
              <CalendarDaysIcon className="size-6" aria-hidden="true" />
            </div>
            <div className="space-y-2">
              <p className="text-sm font-semibold text-primary">Ders programı / Schedule</p>
              <h1 className="text-2xl font-semibold tracking-tight">Program planlayıcı şu anda kullanıma kapalı</h1>
              <p className="text-sm leading-6 text-muted-foreground">
                Yeni üç adımlı planlayıcı kontrollü olarak hazırlanıyor. Etkinleştirildiğinde SAIS doğrulaması,
                ders seçimi ve haftalık program yönetimi burada açılacak.
              </p>
              <p className="text-sm leading-6 text-muted-foreground">
                The new three-step planner is being enabled in a controlled rollout. Once enabled, SAIS verification,
                course selection, and timetable management will appear here.
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <Link href="/settings" className={cn(buttonVariants({ variant: "default" }))}>
                <SettingsIcon />
                Bağlantı ayarlarını aç / Open settings
                <ArrowRightIcon />
              </Link>
              <Link href="/" className={cn(buttonVariants({ variant: "outline" }))}>
                Ana sayfaya dön / Back to home
              </Link>
            </div>
          </CardContent>
        </Card>
      </div>
    </main>
  );
}
