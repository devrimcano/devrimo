"use client";

import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/sonner";
import { LocaleProvider } from "@/components/locale-provider";
import { ThemeProvider } from "next-themes";
import { captureRequestFailure, PostHogAnalytics } from "@/components/posthog-analytics";

/**
 * The name a failed request is reported under.
 *
 * TanStack keys are arrays whose first element is the resource — `["profile"]`,
 * `["campus", "connection"]`. Joining the string-ish head gives a stable
 * operation name to group by; the rest is usually an id and would shatter the
 * grouping.
 */
function operationName(key: readonly unknown[] | undefined, fallback: string) {
  const parts = (key ?? []).filter((part): part is string => typeof part === "string");
  return parts.length ? parts.slice(0, 2).join(".") : fallback;
}

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 10_000,
            retry: 1,
          },
        },
        // Every failed fetch in the app passes through these two, including
        // the ones no component wrapped in a try/catch. Components that report
        // their own failures still do, and the shared reporter deduplicates by
        // error instance so a failure is never counted twice.
        queryCache: new QueryCache({
          onError: (error, query) => {
            captureRequestFailure(error, {
              operation: operationName(query.queryKey, "query"),
              kind: "query",
            });
          },
        }),
        mutationCache: new MutationCache({
          onError: (error, _variables, _context, mutation) => {
            captureRequestFailure(error, {
              operation: operationName(mutation.options.mutationKey, "mutation"),
              kind: "mutation",
            });
          },
        }),
      }),
  );

  return (
    <PostHogAnalytics>
      <ThemeProvider attribute="class" defaultTheme="light" enableSystem={false} storageKey="devrimo-theme">
        <LocaleProvider>
          <QueryClientProvider client={queryClient}>
            <TooltipProvider>
              {children}
              <Toaster />
            </TooltipProvider>
          </QueryClientProvider>
        </LocaleProvider>
      </ThemeProvider>
    </PostHogAnalytics>
  );
}
