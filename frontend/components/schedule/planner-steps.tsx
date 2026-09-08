"use client";

import { CheckCircle2Icon, LockKeyholeIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type PlannerStep = "details" | "courses" | "timetable";

type StepLabels = { tr: string; en: string; hintTr: string; hintEn: string };

const LABELS: Record<PlannerStep, StepLabels> = {
  details: {
    tr: "Bilgilerin",
    en: "Your details",
    hintTr: "Hesabını ve akademik bağlamını doğrula",
    hintEn: "Verify your account and academic context",
  },
  courses: {
    tr: "Derslerin",
    en: "Your courses",
    hintTr: "Bu dönem planlamak istediklerini seç",
    hintEn: "Choose what you want to plan this term",
  },
  timetable: {
    tr: "Programın",
    en: "Your timetable",
    hintTr: "Programını gör, düzenle ve dışa aktar",
    hintEn: "Review, edit, and export your schedule",
  },
};

export function PlannerStepNav({
  active,
  completed,
  canOpen,
  onSelect,
  t,
}: {
  active: PlannerStep;
  completed: Record<PlannerStep, boolean>;
  canOpen: Record<PlannerStep, boolean>;
  onSelect: (step: PlannerStep) => void;
  t: (tr: string, en: string) => string;
}) {
  const steps: PlannerStep[] = ["details", "courses", "timetable"];
  return (
    <nav aria-label={t("Program adımları", "Planner steps")}>
      <ol className="grid gap-2 sm:grid-cols-3">
        {steps.map((step, index) => {
          const label = LABELS[step];
          const selected = step === active;
          const enabled = canOpen[step];
          return (
            <li key={step}>
              <Button
                type="button"
                variant={selected ? "default" : "outline"}
                disabled={!enabled}
                aria-current={selected ? "step" : undefined}
                onClick={() => onSelect(step)}
                className={cn(
                  "h-auto w-full justify-start gap-3 px-3 py-2.5 text-left",
                  selected && "shadow-sm",
                )}
              >
                <span
                  className={cn(
                    "grid size-7 shrink-0 place-items-center rounded-full border text-xs font-semibold",
                    selected ? "border-primary-foreground/50 bg-primary-foreground/15" : "bg-muted",
                  )}
                >
                  {completed[step] && !selected ? <CheckCircle2Icon className="size-4" aria-hidden="true" /> : enabled ? index + 1 : <LockKeyholeIcon className="size-3.5" aria-hidden="true" />}
                </span>
                <span className="min-w-0">
                  <span className="block truncate text-sm font-semibold">{t(label.tr, label.en)}</span>
                  <span className={cn("mt-0.5 block truncate text-[11px] font-normal", selected ? "text-primary-foreground/75" : "text-muted-foreground")}>{t(label.hintTr, label.hintEn)}</span>
                </span>
              </Button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

