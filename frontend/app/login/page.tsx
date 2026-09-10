"use client";

import { Suspense } from "react";
import { isSupabaseConfigured } from "@/lib/env";
import { LoginForm } from "@/components/auth/login-form";
import { BrandMark } from "@/components/brand-mark";
import { BookOpenIcon, CalendarDaysIcon, MapPinnedIcon, SparklesIcon } from "lucide-react";
import { LocaleSwitcher } from "@/components/locale-switcher";
import { useLocale } from "@/components/locale-provider";
import { ThemeSwitcher } from "@/components/theme-switcher";

export default function LoginPage() {
  const { pick } = useLocale();

  return (
    <main id="main-content" tabIndex={-1} className="campus-grid relative min-h-svh overflow-x-hidden px-3 py-3 outline-none sm:px-8 sm:py-8">
      <div className="pointer-events-none absolute -right-28 -top-28 size-96 rounded-full bg-primary/10 blur-3xl" />
      {/* Measured live at 375px: this cluster overlapped the wordmark's subtitle
          by 88x13 CSS px, and a tap on the middle of that subtitle hit the pill.
          It only floats where there is room for it to float. */}
      <div className="relative z-20 mb-3 flex items-center justify-end gap-2 sm:absolute sm:right-12 sm:top-12 sm:mb-0">
        <ThemeSwitcher />
        <LocaleSwitcher />
      </div>
      <div className="motion-enter relative mx-auto grid min-h-[calc(100svh-1.5rem)] min-w-0 max-w-6xl overflow-hidden rounded-2xl border bg-card/90 shadow-[0_24px_80px_rgb(55_37_26/14%)] backdrop-blur-sm dark:shadow-[0_24px_80px_rgb(0_0_0/45%)] sm:min-h-[calc(100svh-4rem)] sm:rounded-[2rem] lg:grid-cols-[1.08fr_0.92fr]">
        {/* Second on a phone, first from lg up. Measured on the live page: at
            375x667 the sign-in card began at 636px of a 667px viewport and its
            button sat 185px below the fold, so someone opening the login page
            on a small phone saw the pitch and no form at all, and had to
            discover by scrolling that there was one. The wide layout is
            unchanged - there the two sit side by side. */}
        <section className="relative order-2 flex min-w-0 flex-col justify-between overflow-hidden bg-secondary p-5 text-foreground lg:order-1 dark:bg-[#181513] dark:text-white sm:p-10 lg:p-14">
          <div className="absolute -bottom-28 -right-24 size-80 rounded-full border-[42px] border-primary/30" />
          <div className="motion-enter relative flex items-center gap-3 [animation-delay:80ms]">
            <BrandMark />
            <div>
              <p className="font-bold leading-none tracking-tight">devrimo</p>
              <p className="mt-1 text-[11px] font-semibold tracking-[0.16em] text-muted-foreground uppercase dark:text-white/55">{pick({ tr: "ODTÜ öğrenci asistanı", en: "AI assistant for METU" })}</p>
            </div>
          </div>

          <div className="motion-enter relative my-10 min-w-0 max-w-xl sm:my-16 lg:my-8 [animation-delay:140ms]">
            <div className="mb-5 inline-flex items-center gap-2 rounded-full border bg-background/55 px-3 py-1.5 text-xs text-muted-foreground dark:border-white/15 dark:bg-white/5 dark:text-white/75">
              <SparklesIcon className="size-3.5 text-primary" />
              {pick({ tr: "Kampüsteki yeni çalışma arkadaşın", en: "Your new campus companion" })}
            </div>
            <h1 className="text-3xl font-semibold leading-[1.04] tracking-[-0.05em] min-[400px]:text-4xl sm:text-5xl lg:text-6xl">
              {pick({ tr: "ODTÜ hayatı,", en: "METU life," })}
              <span className="block text-primary">{pick({ tr: "biraz daha kolay.", en: "made simpler." })}</span>
            </h1>
            <p className="mt-5 max-w-lg text-sm leading-6 text-muted-foreground sm:text-base sm:leading-7 dark:text-white/65">
              {pick({
                tr: "Ders planından kampüste boş sınıf bulmaya kadar, ihtiyacın olan bilgi tek yerde.",
                en: "From planning coursework to finding your way around campus, get the context you need in one place.",
              })}
            </p>
          </div>

          <div className="motion-enter relative grid min-w-0 grid-cols-3 gap-2 [animation-delay:220ms]">
            {[{ icon: BookOpenIcon, label: pick({ tr: "Dersler", en: "Courses" }) }, { icon: CalendarDaysIcon, label: pick({ tr: "Takvim", en: "Calendar" }) }, { icon: MapPinnedIcon, label: pick({ tr: "Kampüs", en: "Campus" }) }].map(({ icon: Icon, label }) => (
              <div key={label} className="min-w-0 rounded-2xl border bg-background/55 p-2.5 text-[11px] text-muted-foreground sm:p-4 sm:text-xs dark:border-white/10 dark:bg-white/[0.04] dark:text-white/70">
                <Icon className="mb-2 size-4 text-primary" />
                {label}
              </div>
            ))}
          </div>
        </section>

        <section className="order-1 flex min-w-0 items-center justify-center p-4 py-8 lg:order-2 sm:p-10 lg:p-14">
          <div className="motion-enter min-w-0 w-full max-w-md [animation-delay:180ms]">
            {isSupabaseConfigured() ? (
              <Suspense>
                <LoginForm />
              </Suspense>
            ) : (
              <div className="rounded-3xl border bg-card/70 p-7 shadow-sm">
                <p className="text-xs font-bold tracking-[0.16em] text-primary uppercase">{pick({ tr: "Kurulum gerekli", en: "Setup required" })}</p>
                <h2 className="mt-3 text-2xl font-semibold tracking-tight">{pick({ tr: "Giriş bağlantısını tamamla", en: "Connect authentication" })}</h2>
                <p className="mt-3 text-sm leading-6 text-muted-foreground">
                  {pick({ tr: "Giriş ekranını etkinleştirmek için Supabase bilgilerini", en: "Add your Supabase credentials to" })} <code className="rounded bg-muted px-1.5 py-0.5 text-foreground">.env.local</code>{pick({ tr: " dosyasına ekle.", en: " to enable sign-in." })}
                </p>
                <div className="mt-5 space-y-2 rounded-2xl border bg-secondary p-4 font-mono text-xs text-secondary-foreground">
                  <p>NEXT_PUBLIC_SUPABASE_URL</p>
                  <p>NEXT_PUBLIC_SUPABASE_ANON_KEY</p>
                </div>
              </div>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
