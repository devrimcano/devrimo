"use client";

import Link from "next/link";
import { PlugZapIcon, RefreshCwIcon, ShieldAlertIcon } from "lucide-react";
import { useCampus } from "@/hooks/useCampus";
import { useLocale } from "@/components/locale-provider";
import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * Whether this student's campus connection can answer right now.
 *
 * The main screen used to say nothing about this. A student whose METU password
 * had rotated saw an ordinary chat, asked for their week, and read "SAIS did not
 * answer" as "I have no classes" — the one reading the product's own rule
 * forbids. The verdict lived in Settings, two navigations away, and nowhere
 * else.
 *
 * Silent by design when the connection is healthy: a banner that is always
 * there is a banner nobody reads.
 */
export function CampusStatusNotice() {
  const { pick } = useLocale();
  const { connection, isLoading } = useCampus();

  // Nothing is claimed before the answer arrives. An optimistic "connected" and
  // an optimistic "broken" are both worse than a quiet moment.
  if (isLoading || !connection) return null;

  const unverified = connection.connected && !connection.verified_at;
  const needsRestart = connection.connected && Boolean(connection.needs_restart);
  if (!unverified && !needsRestart && connection.connected) return null;

  const state = !connection.connected
    ? "disconnected"
    : unverified
      ? "unverified"
      : "restart";

  const Icon = state === "unverified" ? ShieldAlertIcon : state === "restart" ? RefreshCwIcon : PlugZapIcon;

  const headline = pick(
    state === "unverified"
      ? {
          tr: "ODTÜ bağlantın doğrulanamadı",
          en: "Your METU connection could not be verified",
        }
      : state === "restart"
        ? {
            tr: "Bağlantı ayarların değişti",
            en: "Your connection settings changed",
          }
        : {
            tr: "ODTÜ hesabın bağlı değil",
            en: "Your METU account is not connected",
          },
  );

  const explanation = pick(
    state === "unverified"
      ? {
          tr: "Ders programını, transkriptini ve e-postalarını okuyamıyorum. Şifren değiştiyse Ayarlar'dan yeniden bağlanman gerekiyor.",
          en: "I cannot read your schedule, transcript or email. If your password changed, reconnect from Settings.",
        }
      : state === "restart"
        ? {
            tr: "Yeni ayarlar uygulanana kadar kampüs sistemlerinden okuyamıyorum.",
            en: "I cannot read from the campus systems until the new settings are applied.",
          }
        : {
            tr: "Bağlamadığın sürece sana yalnızca genel bilgi verebilirim; derslerin, programın ve postan okunmaz.",
            en: "Until you connect, I can only answer in general: your courses, schedule and mail are not read.",
          },
  );

  // Severity is the difference between "you have not set this up" and "this is
  // set up and it is not working". Only the second is an alert.
  const alert = state !== "disconnected";

  return (
    <div
      role={alert ? "alert" : undefined}
      data-slot="campus-status-notice"
      className={
        alert
          ? "border-destructive/40 bg-destructive/10 text-destructive flex flex-wrap items-start gap-x-3 gap-y-2 rounded-xl border px-3 py-2.5 text-xs leading-5"
          : "border-border bg-card/70 text-muted-foreground flex flex-wrap items-start gap-x-3 gap-y-2 rounded-xl border px-3 py-2.5 text-xs leading-5"
      }
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className={alert ? "font-medium" : "text-foreground font-medium"}>{headline}</p>
        <p className="mt-0.5">{explanation}</p>
        {/* METU's own sentence, kept verbatim: it is the only part a student can
            quote when they report this, and the only part that says which system
            refused. Never the primary message, never hidden either. */}
        {state === "unverified" && connection.verification_error ? (
          <p className="mt-1 break-words">
            {pick({ tr: "ODTÜ yanıtı: ", en: "METU said: " })}
            {connection.verification_error}
          </p>
        ) : null}
      </div>
      <Link
        href="/settings"
        className={cn(
          buttonVariants({ variant: alert ? "default" : "outline", size: "sm" }),
          "min-h-11 shrink-0 rounded-full px-4",
        )}
      >
        {pick(
          state === "disconnected"
            ? { tr: "Hesabını bağla", en: "Connect your account" }
            : { tr: "Yeniden bağlan", en: "Reconnect" },
        )}
      </Link>
    </div>
  );
}
