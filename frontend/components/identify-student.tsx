"use client";

import { useEffect } from "react";
import { identifyStudent } from "@/components/posthog-analytics";

/**
 * Links the browser to the signed-in student on every authenticated page load.
 *
 * Mounted from the authenticated layout rather than only at sign-in, because
 * sign-in is not the only way a session begins — a returning student with a
 * live cookie never passes through the login form at all, and their events
 * would otherwise stay anonymous forever.
 */
export function IdentifyStudent({ userId }: { userId: string }) {
  useEffect(() => {
    identifyStudent(userId);
  }, [userId]);

  return null;
}
