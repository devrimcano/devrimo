"use client";

import { RotateCcwIcon, TriangleAlertIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

export function SettingsQueryError({
  message,
  retryLabel,
  retryingLabel,
  retry,
  retrying = false,
}: {
  message: string;
  retryLabel: string;
  retryingLabel?: string;
  retry: () => void | Promise<unknown>;
  retrying?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-xl border border-destructive/35 bg-destructive/5 p-3 text-sm" role="alert">
      <TriangleAlertIcon className="size-4 shrink-0 text-destructive" aria-hidden="true" />
      <span className="min-w-0 flex-1">{message}</span>
      <Button variant="outline" size="sm" onClick={() => void retry()} disabled={retrying}>
        <RotateCcwIcon className={retrying ? "animate-spin" : undefined} />
        {retrying ? retryingLabel ?? retryLabel : retryLabel}
      </Button>
    </div>
  );
}
