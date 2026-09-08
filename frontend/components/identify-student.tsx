"use client";

import { useEffect } from "react";
import { identifyStudent, type StudentIdentityProperties } from "@/components/posthog-analytics";
import { useCampus } from "@/hooks/useCampus";
import { useProfile } from "@/hooks/useProfile";

/**
 * Links the browser to the signed-in student on every authenticated page load.
 *
 * Mounted from the authenticated layout rather than only at sign-in, because
 * sign-in is not the only way a session begins — a returning student with a
 * live cookie never passes through the login form at all, and their events
 * would otherwise stay anonymous forever.
 */
export function IdentifyStudent({ userId }: { userId: string }) {
  const { profile } = useProfile();
  const { connection } = useCampus();

  useEffect(() => {
    const properties: StudentIdentityProperties = {
      name: profile?.display_name,
      user_name: connection?.metu_username,
      department: profile?.department,
    };
    identifyStudent(userId, properties);
  }, [userId, profile?.display_name, profile?.department, connection?.metu_username]);

  return null;
}
