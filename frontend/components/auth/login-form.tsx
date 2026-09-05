"use client";

import { useState } from "react";
import { useSearchParams } from "next/navigation";
import { Loader2Icon } from "lucide-react";
import { createClient } from "@/lib/supabase/client";
import { getSiteUrl } from "@/lib/env";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { useLocale } from "@/components/locale-provider";
import { captureError, captureProductEvent, identifyStudent } from "@/components/posthog-analytics";

export function LoginForm() {
  const { pick } = useLocale();
  const searchParams = useSearchParams();
  const requestedNext = searchParams.get("next") || "/";
  const next = requestedNext.startsWith("/") && !requestedNext.startsWith("//") ? requestedNext : "/";
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(
    searchParams.get("error") === "auth"
      ? pick({ tr: "Giriş tamamlanamadı.", en: "Could not complete sign in." })
      : null,
  );
  const [info, setInfo] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const errorId = "auth-form-error";
  const infoId = "auth-form-info";

  function authErrorMessage(caught: unknown) {
    const message = caught instanceof Error ? caught.message : pick({ tr: "Kimlik doğrulama başarısız oldu.", en: "Authentication failed." });
    const normalized = message.toLowerCase();
    if (normalized.includes("email not confirmed")) {
      return pick({ tr: "E-posta adresin henüz onaylanmamış. Gelen kutundaki onay bağlantısını açıp tekrar dene.", en: "Your email is not confirmed yet. Open the confirmation link in your inbox and try again." });
    }
    if (normalized.includes("invalid login credentials")) {
      return pick({ tr: "E-posta veya şifre hatalı. Kayıt olduysan e-posta onayını da kontrol et.", en: "The email or password is incorrect. If you just signed up, check your email confirmation too." });
    }
    if (normalized.includes("failed to fetch") || normalized.includes("network")) {
      return pick({ tr: "Giriş hizmetine bağlanılamadı. İnternet bağlantını kontrol edip tekrar dene.", en: "Could not reach the sign-in service. Check your connection and try again." });
    }
    return message;
  }

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setInfo(null);
    setPending(true);

    const supabase = createClient();

    const authMode = mode === "login" ? "sign-in" : "sign-up";
    // The denominator. Without an attempt event a student who cannot sign in
    // is indistinguishable from one who never tried.
    captureProductEvent("auth_submitted", { mode: authMode });

    try {
      if (mode === "login") {
        const { data, error: signInError } = await supabase.auth.signInWithPassword({
          email,
          password,
        });
        if (signInError) throw signInError;
        if (!data.session) throw new Error(pick({ tr: "Giriş tamamlandı ancak oturum oluşturulamadı.", en: "Sign-in completed but no session was created." }));
        // Identified here as well as in the authenticated layout, so the
        // sign-in itself lands on the student rather than on the anonymous
        // device that preceded it.
        identifyStudent(data.session.user.id);
        captureProductEvent("auth_result", { mode: authMode, result: "success", reason: null });
        setInfo(pick({ tr: "Giriş başarılı, asistanın açılıyor…", en: "Signed in. Opening your assistant…" }));
        window.location.replace(next);
        return;
      }

      const origin = getSiteUrl() || window.location.origin;
      const { data, error: signUpError } = await supabase.auth.signUp({
        email,
        password,
        options: {
          emailRedirectTo: `${origin}/auth/callback?next=${encodeURIComponent(next)}`,
        },
      });
      if (signUpError) throw signUpError;
      if (data.session) {
        identifyStudent(data.session.user.id);
        captureProductEvent("auth_result", { mode: authMode, result: "success", reason: null });
        window.location.assign(next);
        return;
      }
      captureProductEvent("auth_result", { mode: authMode, result: "success", reason: "confirmation_email_sent" });
      setInfo(pick({ tr: "Hesabını etkinleştirmek için e-posta adresine gönderdiğimiz bağlantıyı aç.", en: "Open the link we sent to your email to activate your account." }));
    } catch (caught) {
      captureProductEvent("auth_result", {
        mode: authMode,
        result: "error",
        reason: caught instanceof Error ? caught.name : "unknown",
      });
      captureError(caught, { source: "auth", mode });
      setError(authErrorMessage(caught));
    } finally {
      setPending(false);
    }
  }

  return (
    <Card className="min-w-0 w-full max-w-md border-0 bg-transparent py-0 shadow-none">
      <CardHeader>
        <CardTitle className="text-2xl tracking-[-0.04em] sm:text-3xl">{mode === "login" ? pick({ tr: "Tekrar hoş geldin", en: "Welcome back" }) : pick({ tr: "Aramıza katıl", en: "Join Devrimo" })}</CardTitle>
        <CardDescription>
          {mode === "login"
            ? pick({ tr: "Kaldığın yerden devam etmek için hesabına giriş yap.", en: "Sign in to continue with your personal METU assistant." })
            : pick({ tr: "Kişisel kampüs asistanını kullanmaya başla.", en: "Create your personal campus assistant." })}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form className="flex flex-col gap-4" onSubmit={onSubmit}>
          <div className="flex flex-col gap-2">
            <Label htmlFor="email">{pick({ tr: "E-posta", en: "Email" })}</Label>
            <Input
              id="email"
              type="email"
              autoComplete="email"
              required
              aria-invalid={Boolean(error)}
              aria-describedby={error ? errorId : info ? infoId : undefined}
              placeholder="isim@metu.edu.tr"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="password">{pick({ tr: "Şifre", en: "Password" })}</Label>
            <Input
              id="password"
              type="password"
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              required
              minLength={6}
              aria-invalid={Boolean(error)}
              aria-describedby={error ? errorId : info ? infoId : undefined}
              placeholder={pick({ tr: "En az 6 karakter", en: "At least 6 characters" })}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </div>
          {error ? <p id={errorId} role="alert" className="break-words rounded-xl border border-destructive/35 bg-destructive/10 px-3 py-2.5 text-sm text-destructive">{error}</p> : null}
          {info ? <p id={infoId} role="status" aria-live="polite" className="break-words rounded-xl bg-accent px-3 py-2.5 text-sm leading-5 text-accent-foreground">{info}</p> : null}
          <Button type="submit" disabled={pending} className="h-10 w-full shadow-sm">
            {pending ? <Loader2Icon className="animate-spin" /> : null}
            {mode === "login" ? pick({ tr: "Giriş yap", en: "Sign in" }) : pick({ tr: "Hesap oluştur", en: "Create account" })}
          </Button>
        </form>
        <p className="mt-4 text-center text-sm text-muted-foreground">
          {mode === "login" ? pick({ tr: "Henüz hesabın yok mu?", en: "New to Devrimo?" }) : pick({ tr: "Zaten hesabın var mı?", en: "Already have an account?" })}{" "}
          <button
            type="button"
            className="font-medium text-foreground underline-offset-4 hover:underline"
            onClick={() => {
              setMode(mode === "login" ? "signup" : "login");
              setError(null);
              setInfo(null);
            }}
          >
            {mode === "login" ? pick({ tr: "Kayıt ol", en: "Create account" }) : pick({ tr: "Giriş yap", en: "Sign in" })}
          </button>
        </p>
      </CardContent>
    </Card>
  );
}
