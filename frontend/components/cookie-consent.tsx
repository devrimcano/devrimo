"use client";

import Link from "next/link";
import { useMemo, useState, useSyncExternalStore } from "react";
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
} from "@/lib/consent";

/**
 * The question, asked once, before anything optional is written.
 *
 * Three things about the shape of this are requirements rather than taste, all
 * from KVKK's cookie guidance:
 *
 *   - Refusing is exactly as easy as accepting: one click either way, same
 *     size, same weight, side by side, at the same step. A dimmed "reject"
 *     beside a bright "accept", or a reject reachable only through a settings
 *     panel, is not a choice.
 *   - The optional categories are switchable separately, so agreeing to be
 *     counted is not the same act as agreeing to be recorded.
 *   - Nothing optional runs while the question is open. There is no "by
 *     continuing you accept": silence is not consent, so measurement does not
 *     start until someone says so.
 *
 * The cookies the site cannot work without are not on offer here at all, and
 * cannot be switched off — they rest on a legal ground other than consent, and
 * pretending otherwise would be offering a choice that does not exist. What
 * they are, and why, is on the page this links to.
 *
 * A bar rather than a modal, because a modal makes reading the notice
 * conditional on answering the question the notice exists to inform.
 */
export function CookieConsent() {
  const { pick } = useLocale();
  const raw = useSyncExternalStore(subscribeConsent, consentSnapshot, consentServerSnapshot);
  const consent = useMemo(() => parseConsent(raw), [raw]);

  const [open, setOpen] = useState(false);
  const [measurement, setMeasurement] = useState(true);
  const [replay, setReplay] = useState(true);

  function answer(categories: { measurement: boolean; replay: boolean }) {
    writeConsent(categories);
    applyConsent(categories);
  }

  // `null` is the server render, where no cookie is readable; `decided` is
  // someone who answered already. Neither is asked again.
  if (!consent || consent.decided) return null;

  return (
    <div
      role="region"
      aria-label={pick({ tr: "Çerez tercihi", en: "Cookie choice" })}
      className="fixed inset-x-0 bottom-0 z-50 flex justify-center p-3 sm:p-4"
    >
      <div className="bg-popover text-popover-foreground border-border w-full max-w-3xl rounded-xl border p-4 shadow-lg">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:gap-4">
          <div className="min-w-0 flex-1">
            <p className="text-foreground text-sm font-medium">
              {pick({ tr: "Çerezler hakkında kısa bir soru", en: "A quick question about cookies" })}
            </p>
            <p className="text-muted-foreground mt-1 text-xs leading-5">
              {pick({
                tr: "Girişini hatırlayan birkaç çerez zaten var, onlar şart. Bunun dışında, neyin işine yaradığını sayan ve bir şey bozulduğunda bize haber veren çerezler için iznini istiyoruz. İstemezsen site hiçbir şey kaybetmez.",
                en: "A few cookies keep you signed in; those are essential. Beyond that we'd like permission for cookies that count what's useful and tell us when something breaks. Say no and the site loses nothing.",
              })}{" "}
              <Link href="/gizlilik" className="text-primary underline underline-offset-4">
                {pick({ tr: "Çerezler ve gizlilik", en: "Cookies and privacy" })}
              </Link>
            </p>
            <button
              type="button"
              onClick={() => setOpen((value) => !value)}
              aria-expanded={open}
              className="text-muted-foreground hover:text-foreground mt-2 text-xs underline underline-offset-4"
            >
              {pick({ tr: "Tek tek seçmek isterim", en: "Let me choose one by one" })}
            </button>
          </div>

          {/* Same variant, same size, same step: the refusal is not the quieter
              button, and it is not one level deeper. */}
          <div className="flex shrink-0 gap-2">
            <Button
              size="sm"
              variant="outline"
              className="flex-1 sm:flex-none"
              onClick={() => answer({ measurement: false, replay: false })}
            >
              {pick({ tr: "Gerek yok", en: "No thanks" })}
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="flex-1 sm:flex-none"
              onClick={() => answer(open ? { measurement, replay } : { measurement: true, replay: true })}
            >
              {open ? pick({ tr: "Kaydet", en: "Save" }) : pick({ tr: "Olur", en: "Sure" })}
            </Button>
          </div>
        </div>

        {open ? (
          <div className="border-border mt-3 flex flex-col gap-3 border-t pt-3">
            <ConsentRow
              label={pick({ tr: "Neyin işe yaradığını sayalım", en: "Count what's useful" })}
              description={pick({
                tr: "Hangi sayfaların kullanıldığı, bir ekran çöktüğünde haberimizin olması.",
                en: "Which pages get used, and a heads-up when a screen crashes.",
              })}
              checked={measurement}
              onChange={(next) => {
                setMeasurement(next);
                // Replay is a recording the measurement SDK makes; without it
                // there is nothing to record with.
                if (!next) setReplay(false);
              }}
            />
            <ConsentRow
              label={pick({ tr: "Bozulduğunda geri sarabilelim", en: "Let us rewind a problem" })}
              description={pick({
                tr: "Bir şey ters gittiğinde ne olduğunu görebilmek için oturum kaydı. Şifreler kayda girmez.",
                en: "A session recording, so we can see what went wrong. Passwords never enter it.",
              })}
              checked={replay}
              disabled={!measurement}
              onChange={setReplay}
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}

function ConsentRow({
  label,
  description,
  checked,
  disabled,
  onChange,
}: {
  label: string;
  description: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <p className="text-xs font-medium">{label}</p>
        <p className="text-muted-foreground text-xs leading-5">{description}</p>
      </div>
      <Switch aria-label={label} checked={checked} disabled={disabled} onCheckedChange={onChange} />
    </div>
  );
}
