"use client";

import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";
import { Button } from "@/components/ui/button";
import { useLocale } from "@/components/locale-provider";
import { captureError, captureProductEvent, resetStudent } from "@/components/posthog-analytics";

export function SignOutButton() {
  const { pick } = useLocale();
  const router = useRouter();

  async function signOut() {
    const supabase = createClient();
    try {
      const { error } = await supabase.auth.signOut();
      if (error) throw error;
      // Captured before reset(), so the event belongs to the student who
      // signed out rather than to the anonymous device left behind.
      captureProductEvent("auth_signed_out", { result: "success" });
    } catch (error) {
      // A sign-out that fails leaves a session alive on what may be a shared
      // machine. It used to be swallowed entirely.
      captureProductEvent("auth_signed_out", { result: "error" });
      captureError(error, { source: "auth_sign_out" });
    }
    // Unconditional: on a shared machine the next person to sign in would
    // otherwise inherit this student's identity and their events.
    resetStudent();
    router.replace("/login");
    router.refresh();
  }

  return (
    <Button variant="outline" size="sm" onClick={signOut}>
      {pick({ tr: "Çıkış", en: "Sign out" })}
    </Button>
  );
}
