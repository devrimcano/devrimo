"use client";

import { useEffect } from "react";
import { Button } from "@/components/ui/button";
import { useLocale } from "@/components/locale-provider";
import { captureError } from "@/components/posthog-analytics";

/**
 * The route-level error boundary the app has never had.
 *
 * Without this, a render error blanks the page and leaves no record anywhere.
 */
export default function RouteError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const { pick } = useLocale();
  useEffect(() => {
    captureError(error, { source: "route_error_boundary", digest: error.digest ?? null });
  }, [error]);

  return (
    <div role="alert" className="flex h-full min-h-svh flex-col items-center justify-center gap-4 px-6 text-center">
      <div>
        <p className="font-medium">{pick({ tr: "Bir şeyler ters gitti", en: "Something went wrong" })}</p>
        <p className="mt-1 max-w-md text-sm text-muted-foreground">
          {pick({
            tr: "Bu sayfa yüklenemedi. Tekrar denemek sorunu genellikle çözer.",
            en: "This page could not load. Trying again usually fixes it.",
          })}
        </p>
      </div>
      <Button onClick={reset}>{pick({ tr: "Tekrar dene", en: "Try again" })}</Button>
    </div>
  );
}
