"use client";

import { useMemo, useSyncExternalStore } from "react";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { useLocale } from "@/components/locale-provider";
import { applyConsent } from "@/lib/analytics";
import {
  consentServerSnapshot,
  consentSnapshot,
  parseConsent,
  subscribeConsent,
  writeConsent,
  type ConsentCategories,
} from "@/lib/consent";

/**
 * Changing the answer, wherever the question is revisited.
 *
 * Withdrawal has to be as easy as consent was, and "as easy" includes findable:
 * the banner is answered once and never seen again, so this is where the answer
 * lives afterwards. Shown on the notice page and in settings; both read the
 * same store, so a change in one shows in the other without a reload.
 *
 * The cookies the site cannot work without have no switch here, on purpose. A
 * toggle that cannot be turned off is worse than no toggle: it implies a choice
 * that was never on offer. They are listed, and explained, in the notice.
 */
export function ConsentPreference() {
  const { pick } = useLocale();
  const raw = useSyncExternalStore(subscribeConsent, consentSnapshot, consentServerSnapshot);
  const consent = useMemo(() => parseConsent(raw), [raw]);

  function answer(categories: ConsentCategories) {
    writeConsent(categories);
    applyConsent(categories);
  }

  if (!consent) return null;

  const state = !consent.decided
    ? pick({
        tr: "Henüz bir tercih belirtmedin; isteğe bağlı çerezlerin hiçbiri yüklenmiyor.",
        en: "You have not chosen yet; none of the optional cookies are loaded.",
      })
    : consent.measurement
      ? consent.replay
        ? pick({ tr: "Ölçüm ve oturum kaydı açık.", en: "Measurement and session recording are on." })
        : pick({ tr: "Ölçüm açık, oturum kaydı kapalı.", en: "Measurement is on, session recording is off." })
      : pick({ tr: "İsteğe bağlı çerezlerin hiçbiri kullanılmıyor.", en: "None of the optional cookies are in use." });

  return (
    <div className="border-border flex flex-col gap-3 rounded-lg border p-3">
      <p className="text-sm">{state}</p>

      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium">
            {pick({ tr: "Kullanım ölçümü ve hata takibi", en: "Usage measurement and error tracking" })}
          </p>
          <p className="text-muted-foreground text-xs leading-5">
            {pick({
              tr: "Hangi sayfaların kullanıldığını sayar ve çöken ekranları yakalar.",
              en: "Counts which pages get used and catches screens that crash.",
            })}
          </p>
        </div>
        <Switch
          aria-label={pick({ tr: "Kullanım ölçümü ve hata takibi", en: "Usage measurement and error tracking" })}
          checked={consent.measurement}
          onCheckedChange={(next) => answer({ measurement: next, replay: next && consent.replay })}
        />
      </div>

      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium">{pick({ tr: "Oturum kaydı", en: "Session recording" })}</p>
          <p className="text-muted-foreground text-xs leading-5">
            {pick({
              tr: "Ekranındaki hareketleri kaydeder. Parola alanları kayıtta maskelenir.",
              en: "Records what happens on your screen. Password fields are masked.",
            })}
          </p>
        </div>
        <Switch
          aria-label={pick({ tr: "Oturum kaydı", en: "Session recording" })}
          checked={consent.replay}
          disabled={!consent.measurement}
          onCheckedChange={(next) => answer({ measurement: consent.measurement, replay: next })}
        />
      </div>

      {consent.decided && (consent.measurement || consent.replay) ? (
        <div>
          <Button size="sm" variant="outline" onClick={() => answer({ measurement: false, replay: false })}>
            {pick({ tr: "Hepsini kapat ve verileri sil", en: "Turn everything off and delete the data" })}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
