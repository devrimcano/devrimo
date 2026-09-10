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
        tr: "Henüz seçmedin — o yüzden isteğe bağlı hiçbir çerez yüklenmiyor.",
        en: "You haven't chosen yet — so no optional cookies are loaded.",
      })
    : consent.measurement
      ? consent.replay
        ? pick({ tr: "İkisi de açık. Teşekkürler, gerçekten yardımı oluyor.", en: "Both are on. Thank you — it genuinely helps." })
        : pick({ tr: "Sayım açık, kayıt kapalı.", en: "Counting is on, recording is off." })
      : pick({ tr: "Hepsi kapalı. Site yine tam çalışıyor.", en: "All off. The site still works fully." });

  return (
    <div className="border-border flex flex-col gap-3 rounded-lg border p-3">
      <p className="text-sm">{state}</p>

      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium">
            {pick({ tr: "Neyin işe yaradığını sayalım", en: "Count what's useful" })}
          </p>
          <p className="text-muted-foreground text-xs leading-5">
            {pick({
              tr: "Hangi sayfaların kullanıldığı, bir ekran çöktüğünde haberimizin olması.",
              en: "Which pages get used, and a heads-up when a screen crashes.",
            })}
          </p>
        </div>
        <Switch
          aria-label={pick({ tr: "Neyin işe yaradığını sayalım", en: "Count what's useful" })}
          checked={consent.measurement}
          onCheckedChange={(next) => answer({ measurement: next, replay: next && consent.replay })}
        />
      </div>

      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium">
            {pick({ tr: "Bozulduğunda geri sarabilelim", en: "Let us rewind a problem" })}
          </p>
          <p className="text-muted-foreground text-xs leading-5">
            {pick({
              tr: "Bir şey ters gittiğinde ne olduğunu görebilmek için oturum kaydı. Şifreler kayda girmez.",
              en: "A session recording, so we can see what went wrong. Passwords never enter it.",
            })}
          </p>
        </div>
        <Switch
          aria-label={pick({ tr: "Bozulduğunda geri sarabilelim", en: "Let us rewind a problem" })}
          checked={consent.replay}
          disabled={!consent.measurement}
          onCheckedChange={(next) => answer({ measurement: consent.measurement, replay: next })}
        />
      </div>

      {consent.decided && (consent.measurement || consent.replay) ? (
        <div>
          <Button size="sm" variant="outline" onClick={() => answer({ measurement: false, replay: false })}>
            {pick({ tr: "Hepsini kapat, bıraktıklarını da sil", en: "Turn it all off and delete what's left" })}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
