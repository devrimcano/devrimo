"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { EyeIcon, EyeOffIcon, Loader2Icon, ShieldCheckIcon } from "lucide-react";
import { createClient } from "@/lib/supabase/client";
import { useLocale } from "@/components/locale-provider";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { captureError, captureProductEvent } from "@/components/posthog-analytics";

/**
 * Where a password-reset link lands.
 *
 * The email points at `/auth/callback`, which exchanges the recovery code for a
 * session and forwards here, so by the time this renders the visitor is signed
 * in for exactly one purpose: choosing a new password. A visitor who arrives
 * without that session - an expired link, a link opened in another browser, a
 * bookmark - is told so and sent back to ask for a new one, rather than shown a
 * form that cannot work.
 */
export default function NewPasswordPage() {
  const { pick } = useLocale();
  const router = useRouter();
  const [ready, setReady] = useState<"checking" | "ready" | "expired">("checking");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [visible, setVisible] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [done, setDone] = useState(false);
  const errorId = "new-password-error";

  useEffect(() => {
    let cancelled = false;
    const supabase = createClient();
    void supabase.auth.getSession().then(({ data }) => {
      if (cancelled) return;
      setReady(data.session ? "ready" : "expired");
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);

    if (password.length < 6) {
      setError(pick({ tr: "Şifre en az 6 karakter olmalı.", en: "The password must be at least 6 characters." }));
      return;
    }
    if (password !== confirmation) {
      setError(pick({ tr: "İki şifre birbirini tutmuyor.", en: "The two passwords do not match." }));
      return;
    }

    setPending(true);
    captureProductEvent("auth_submitted", { mode: "new-password" });
    try {
      const supabase = createClient();
      const { error: updateError } = await supabase.auth.updateUser({ password });
      if (updateError) throw updateError;
      captureProductEvent("auth_result", { mode: "new-password", result: "success", reason: null });
      setDone(true);
      // Straight into the app: the session from the recovery link is already
      // this student's, and the password they just set is the one that works.
      window.setTimeout(() => router.push("/"), 900);
    } catch (caught) {
      const message = caught instanceof Error
        ? caught.message
        : pick({ tr: "Şifre değiştirilemedi.", en: "The password could not be changed." });
      captureProductEvent("auth_result", { mode: "new-password", result: "error", reason: message.slice(0, 80) });
      captureError(caught, { source: "auth_new_password" });
      setError(message);
    } finally {
      setPending(false);
    }
  }

  return (
    <main id="main-content" tabIndex={-1} className="campus-grid flex min-h-svh items-center justify-center px-4 py-10 outline-none">
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle className="text-2xl tracking-[-0.03em]">
            {pick({ tr: "Yeni şifreni belirle", en: "Set a new password" })}
          </CardTitle>
          <CardDescription>
            {ready === "expired"
              ? pick({
                  tr: "Bu bağlantı geçersiz ya da süresi dolmuş. Giriş ekranından yeni bir sıfırlama bağlantısı iste.",
                  en: "This link is invalid or has expired. Ask for a new reset link from the sign-in screen.",
                })
              : pick({
                  tr: "Bundan sonra ODTÜ hesabına değil, Devrimo hesabına bu şifreyle gireceksin.",
                  en: "This is your Devrimo password, not your METU one.",
                })}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {ready === "checking" ? (
            <p className="text-muted-foreground flex items-center gap-2 text-sm" role="status">
              <Loader2Icon className="size-4 animate-spin motion-reduce:animate-none" />
              {pick({ tr: "Bağlantı kontrol ediliyor…", en: "Checking the link…" })}
            </p>
          ) : ready === "expired" ? (
            <Button className="h-11 w-full" onClick={() => router.push("/login")}>
              {pick({ tr: "Giriş ekranına dön", en: "Back to sign in" })}
            </Button>
          ) : done ? (
            <p className="text-success flex items-center gap-2 text-sm" role="status">
              <ShieldCheckIcon className="size-4 shrink-0" />
              {pick({ tr: "Şifren değişti, giriş yapılıyor…", en: "Your password is changed, signing you in…" })}
            </p>
          ) : (
            <form className="flex flex-col gap-4" onSubmit={onSubmit}>
              <div className="flex flex-col gap-2">
                <Label htmlFor="new-password">{pick({ tr: "Yeni şifre", en: "New password" })}</Label>
                <div className="relative">
                  <Input
                    id="new-password"
                    type={visible ? "text" : "password"}
                    autoComplete="new-password"
                    required
                    minLength={6}
                    className="h-11 pr-12"
                    aria-invalid={Boolean(error)}
                    aria-describedby={error ? errorId : undefined}
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                  />
                  <button
                    type="button"
                    onClick={() => setVisible((current) => !current)}
                    className="text-muted-foreground hover:text-foreground focus-visible:ring-ring absolute right-1 top-1/2 grid size-11 -translate-y-1/2 place-items-center rounded-full focus-visible:ring-2 focus-visible:outline-none"
                    aria-label={visible
                      ? pick({ tr: "Şifreyi gizle", en: "Hide password" })
                      : pick({ tr: "Şifreyi göster", en: "Show password" })}
                    aria-pressed={visible}
                  >
                    {visible ? <EyeOffIcon className="size-4" /> : <EyeIcon className="size-4" />}
                  </button>
                </div>
                <p className="text-muted-foreground text-xs">{pick({ tr: "En az 6 karakter.", en: "At least 6 characters." })}</p>
              </div>

              <div className="flex flex-col gap-2">
                <Label htmlFor="confirm-password">{pick({ tr: "Yeni şifre (tekrar)", en: "New password again" })}</Label>
                <Input
                  id="confirm-password"
                  type={visible ? "text" : "password"}
                  autoComplete="new-password"
                  required
                  minLength={6}
                  className="h-11"
                  aria-invalid={Boolean(error)}
                  aria-describedby={error ? errorId : undefined}
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                />
              </div>

              {error ? (
                <p id={errorId} role="alert" className="border-destructive/35 bg-destructive/10 text-destructive break-words rounded-xl border px-3 py-2.5 text-sm">
                  {error}
                </p>
              ) : null}

              <Button type="submit" disabled={pending} className="h-11 w-full">
                {pending ? <Loader2Icon className="animate-spin motion-reduce:animate-none" /> : null}
                {pick({ tr: "Şifreyi değiştir", en: "Change password" })}
              </Button>
            </form>
          )}

          <p className="text-muted-foreground mt-4 text-center text-sm">
            <Link href="/login" className="text-foreground underline-offset-4 hover:underline">
              {pick({ tr: "Giriş ekranı", en: "Sign in" })}
            </Link>
          </p>
        </CardContent>
      </Card>
    </main>
  );
}
