"use client";

import { useEffect, useState } from "react";
import {
  ArrowLeftIcon,
  ArrowRightIcon,
  BookOpenCheckIcon,
  CheckCircle2Icon,
  GraduationCapIcon,
  Loader2Icon,
  LockIcon,
  MailIcon,
  ShieldCheckIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { BrandMark } from "@/components/brand-mark";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useLocale } from "@/components/locale-provider";
import { useCampus } from "@/hooks/useCampus";
import { useProfile } from "@/hooks/useProfile";
import { DataAccessChoice } from "@/components/settings/data-access-choice";
import { StepIndicator } from "@/components/onboarding/step-indicator";
import { ONBOARDING_STEPS, stepIndex, type OnboardingStep } from "@/components/onboarding/steps";
import { captureError, captureProductEvent } from "@/components/posthog-analytics";

const CORE_ACCESS = ["sais", "course_info"];

type PrivacyChoices = {
  odtuclass: boolean;
  webmail: boolean;
};

const STARTER_PROMPTS = {
  tr: [
    "Programımdaki boşluklara göre haftalık çalışma planı yap",
    "Bu dönem yaklaşan önemli akademik tarihleri sırala",
    "CENG 334 için ön koşulları ve açılan şubeleri karşılaştır",
    "Bu akşam sessiz çalışabileceğim kampüs seçeneklerini öner",
  ],
  en: [
    "Build a weekly study plan around the gaps in my schedule",
    "List the important academic dates coming up this semester",
    "Compare the prerequisites and available sections for CENG 334",
    "Suggest quiet places on campus where I can study tonight",
  ],
};

/**
 * The first-run wizard.
 *
 * Progress is written to the profile after each step rather than only at the
 * end, so closing the tab halfway through resumes here instead of starting
 * over. The METU password is held in component state and sent exactly once,
 * on the connect step — it is never written to the profile, to localStorage,
 * or back into any field after submission.
 */
export function OnboardingFlow({ onDone }: { onDone?: () => void }) {
  const { pick, locale, setLocale } = useLocale();
  const { profile, update } = useProfile();
  const { connection, connect } = useCampus();

  // Every field below is draft-or-server: `null` means the student hasn't
  // touched it, so the stored value shows through. Derived rather than synced
  // in an effect, which would overwrite whatever they were typing on each
  // background refetch of the profile or connection.
  const [stepOverride, setStepOverride] = useState<OnboardingStep | null>(null);
  const [displayNameDraft, setDisplayNameDraft] = useState<string | null>(null);
  const [usernameDraft, setUsernameDraft] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [privacyDraft, setPrivacyDraft] = useState<PrivacyChoices | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [warning, setWarning] = useState<string | null>(null);

  // Resuming where the student left off falls out of this for free: the
  // stored step is the starting point until they navigate.
  const step = stepOverride ?? ONBOARDING_STEPS[stepIndex(profile?.onboarding_step)];
  const displayName = displayNameDraft ?? profile?.display_name ?? "";
  const username = usernameDraft ?? connection?.metu_username ?? "";

  const savedPrivacy: PrivacyChoices = {
    odtuclass: connection?.connected ? connection.enabled_tools.includes("odtuclass") : false,
    webmail: connection?.connected ? connection.enabled_tools.includes("webmail") : false,
  };
  const privacy = privacyDraft ?? savedPrivacy;
  const enabledAccess = [
    ...CORE_ACCESS,
    ...(privacy.odtuclass ? ["odtuclass"] : []),
    ...(privacy.webmail ? ["webmail"] : []),
  ];

  const index = ONBOARDING_STEPS.indexOf(step);
  const busy = connect.isPending || update.isPending;

  useEffect(() => {
    captureProductEvent("onboarding_step_viewed", { step });
  }, [step]);

  function goTo(next: OnboardingStep) {
    setFormError(null);
    setStepOverride(next);
    // Fire-and-forget: losing a step marker is not worth blocking the UI on,
    // and the next write will correct it.
    update.mutate({ onboarding_step: next });
  }

  async function submitConnection(skipVerification = false) {
    setFormError(null);
    setWarning(null);

    if (!username.trim()) {
      setFormError(pick({ tr: "ODTÜ kullanıcı adını gir.", en: "Enter your METU username." }));
      return;
    }
    if (!password && !connection?.has_password) {
      setFormError(pick({ tr: "ODTÜ şifreni gir.", en: "Enter your METU password." }));
      return;
    }

    try {
      const result = await connect.mutateAsync({
        metu_username: username.trim(),
        ...(password ? { metu_password: password } : {}),
        locale,
        enabled_tools: enabledAccess,
        skip_verification: skipVerification,
      });
      // Never keep the password around after it has been stored.
      setPassword("");
      captureProductEvent("onboarding_connection_result", {
        result: "success",
        verification_skipped: skipVerification,
      });
      captureProductEvent("campus_connection_saved", {
        source: "onboarding",
        result: "success",
        verification_skipped: skipVerification,
      });
      if (!result.verified_at && result.verification_error) {
        setWarning(result.verification_error);
      }
      goTo("privacy");
    } catch (error) {
      captureProductEvent("onboarding_connection_result", {
        result: "error",
        verification_skipped: skipVerification,
      });
      captureProductEvent("campus_connection_saved", {
        source: "onboarding",
        result: "error",
        verification_skipped: skipVerification,
      });
      captureError(error, { source: "onboarding_connect" });
      setFormError(error instanceof Error ? error.message : "Could not save your METU connection.");
    }
  }

  async function savePrivacyChoices() {
    setFormError(null);
    if (!connection?.connected) {
      goTo("ready");
      return;
    }
    try {
      await connect.mutateAsync({
        metu_username: username.trim() || connection.metu_username || "",
        locale,
        enabled_tools: enabledAccess,
      });
      captureProductEvent("onboarding_tool_selection_saved", {
        tool_count: enabledAccess.length,
        result: "success",
      });
      captureProductEvent("campus_tools_changed", {
        source: "onboarding",
        tool_count: enabledAccess.length,
        result: "success",
      });
      goTo("ready");
    } catch (error) {
      captureProductEvent("onboarding_tool_selection_saved", {
        tool_count: enabledAccess.length,
        result: "error",
      });
      captureProductEvent("campus_tools_changed", {
        source: "onboarding",
        tool_count: enabledAccess.length,
        result: "error",
      });
      captureError(error, { source: "onboarding_tool_selection" });
      setFormError(error instanceof Error ? error.message : "Could not save your data-access choices.");
    }
  }

  async function finish() {
    setFormError(null);
    try {
      await update.mutateAsync({
        display_name: displayName.trim() || null,
        locale,
        onboarding_step: "ready",
        onboarding_completed: true,
      });
      captureProductEvent("onboarding_finished", { path: "completed" });
      onDone?.();
    } catch (error) {
      captureError(error, { source: "onboarding_finish" });
      setFormError(error instanceof Error ? error.message : "Could not finish setup.");
    }
  }

  async function skipSetup() {
    await update.mutateAsync({ onboarding_completed: true, onboarding_step: "ready" });
    captureProductEvent("onboarding_finished", { path: "skipped" });
    onDone?.();
  }

  return (
    <div className="mx-auto flex h-full w-full max-w-xl flex-col overflow-y-auto px-5 py-8">
      <div className="mb-7 flex flex-col items-center gap-4">
        <BrandMark className="size-11 text-sm" />
        <StepIndicator current={index} />
      </div>

      {step === "welcome" ? (
        <section className="flex flex-1 flex-col">
          <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">
            {pick({ tr: "Devrimo'ya hoş geldin", en: "Welcome to Devrimo" })}
          </h1>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            {pick({
              tr: "Devrimo, ODTÜ kampüs hayatı için kişisel bir asistan. Konuşmaların ve bağlantıların yalnızca sana ait — hiçbir şey başka bir öğrenciyle paylaşılmaz.",
              en: "Devrimo is a personal assistant for METU campus life. Your conversations and connections stay private to you and are never shared with another student.",
            })}
          </p>
          <p className="mt-3 text-sm leading-6 text-muted-foreground">
            {pick({
              tr: "Sırada ODTÜ hesabını bağlamak var. Bu, asistanın ders programın, transkriptin, ODTÜClass duyuruların ve e-postan gibi sana özel bilgilere ulaşmasını sağlar.",
              en: "Next you'll connect your METU account. That's what lets the assistant reach your own schedule, transcript, ODTÜClass announcements, and mail.",
            })}
          </p>

          <div className="mt-6 flex flex-col gap-2">
            <Label htmlFor="display-name">
              {pick({ tr: "Sana nasıl hitap edelim? (isteğe bağlı)", en: "What should we call you? (optional)" })}
            </Label>
            <Input
              id="display-name"
              value={displayName}
              autoComplete="given-name"
              placeholder={pick({ tr: "Deniz", en: "Deniz" })}
              onChange={(event) => setDisplayNameDraft(event.target.value)}
            />
          </div>

          <div className="mt-5 flex flex-col gap-2">
            <Label>{pick({ tr: "Dil", en: "Language" })}</Label>
            <div className="flex gap-2">
              {(["tr", "en"] as const).map((option) => (
                <Button
                  key={option}
                  type="button"
                  variant={locale === option ? "default" : "outline"}
                  size="sm"
                  onClick={() => setLocale(option)}
                >
                  {option === "tr" ? "Türkçe" : "English"}
                </Button>
              ))}
            </div>
          </div>

          <Footer
            onNext={() => goTo("connect")}
            nextLabel={pick({ tr: "Devam et", en: "Continue" })}
            onSkip={skipSetup}
            skipLabel={pick({ tr: "Şimdilik geç", en: "Skip for now" })}
            busy={busy}
          />
        </section>
      ) : null}

      {step === "connect" ? (
        <section className="flex flex-1 flex-col">
          <h1 className="text-2xl font-semibold tracking-tight">
            {pick({ tr: "ODTÜ hesabını bağla", en: "Connect your METU account" })}
          </h1>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            {pick({
              tr: "Ders programın, transkriptin ve ders kataloğun için ODTÜ hesabını bağlayabilirsin. ODTÜClass ve e-posta erişimini sonraki adımda ayrıca seçersin.",
              en: "Connect your METU account for your schedule, transcript, and course catalog. You'll choose ODTÜClass and email access separately in the next step.",
            })}
          </p>

          <div className="mt-4 flex items-start gap-2.5 rounded-xl border border-border bg-muted/40 p-3.5">
            <LockIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <div className="text-xs leading-5 text-muted-foreground">
              <p>
                {pick({
                  tr: "Şifren güvenli şekilde saklanır ve yalnızca ODTÜ bağlantın için kullanılır. Kimseyle paylaşılmaz.",
                  en: "Your password is stored securely and used only for your METU connection. It is never shared.",
                })}
              </p>
              <p className="mt-2">
                {pick({
                  tr: "İstediğin zaman Ayarlar'dan bağlantıyı kaldırabilirsin; şifren silinir.",
                  en: "You can disconnect at any time from Settings, which deletes the stored password.",
                })}
              </p>
            </div>
          </div>

          <form
            className="mt-5 flex flex-col gap-4"
            onSubmit={(event) => {
              event.preventDefault();
              void submitConnection();
            }}
          >
            <div className="flex flex-col gap-2">
              <Label htmlFor="metu-username">{pick({ tr: "ODTÜ kullanıcı adı", en: "METU username" })}</Label>
              <Input
                id="metu-username"
                value={username}
                required
                autoComplete="username"
                spellCheck={false}
                placeholder="e123456"
                // The failure belongs to these two fields, so it is announced
                // from them: a paragraph further down the form was invisible to
                // a screen reader and unreachable from the input.
                aria-invalid={formError ? true : undefined}
                aria-describedby={formError ? "metu-credentials-error" : undefined}
                onChange={(event) => setUsernameDraft(event.target.value)}
              />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="metu-password">{pick({ tr: "ODTÜ şifresi", en: "METU password" })}</Label>
              <Input
                id="metu-password"
                type="password"
                value={password}
                autoComplete="current-password"
                placeholder={
                  connection?.has_password
                    ? pick({ tr: "Kayıtlı — değiştirmek için yaz", en: "Stored — type to replace" })
                    : "••••••••"
                }
                aria-invalid={formError ? true : undefined}
                aria-describedby={formError ? "metu-credentials-error" : undefined}
                onChange={(event) => setPassword(event.target.value)}
              />
            </div>

            {formError ? (
              <p
                id="metu-credentials-error"
                role="alert"
                className="flex items-start gap-2 text-sm text-destructive"
              >
                <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" />
                <span className="min-w-0 break-words">{formError}</span>
              </p>
            ) : null}

            <div className="flex flex-wrap items-center justify-between gap-3 pt-1">
              <Button type="button" variant="ghost" size="sm" onClick={() => goTo("welcome")} disabled={busy}>
                <ArrowLeftIcon /> {pick({ tr: "Geri", en: "Back" })}
              </Button>
              <div className="flex items-center gap-2">
                <Button type="button" variant="ghost" size="sm" onClick={() => goTo("privacy")} disabled={busy}>
                  {pick({ tr: "Şimdilik geç", en: "Skip for now" })}
                </Button>
                <Button type="submit" disabled={busy}>
                  {connect.isPending ? <Loader2Icon className="animate-spin" /> : null}
                  {pick({ tr: "Bağlan", en: "Connect" })}
                </Button>
              </div>
            </div>
          </form>
        </section>
      ) : null}

      {step === "privacy" ? (
        <section className="flex flex-1 flex-col">
          <div className="flex items-center gap-2 text-primary"><ShieldCheckIcon className="size-5" /><span className="text-xs font-semibold tracking-[0.16em] uppercase">{pick({ tr: "Gizlilik seçimi", en: "Privacy choices" })}</span></div>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight">{pick({ tr: "Hangi bilgilere erişebilir?", en: "Which information can be used?" })}</h1>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            {pick({
              tr: "Ders programı ve akademik kayıt bağlantının temel parçasıdır. ODTÜClass ve e-posta ise tamamen isteğe bağlıdır.",
              en: "Schedule and academic records are the core connection. ODTÜClass and email are entirely optional.",
            })}
          </p>

          {warning ? (
            <p className="mt-4 flex items-start gap-2 rounded-xl bg-amber-500/10 p-3 text-xs leading-5 text-amber-700 dark:text-amber-400">
              <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" />
              <span className="min-w-0 break-words">{warning}</span>
            </p>
          ) : null}

          {!connection?.connected ? (
            <p className="mt-4 rounded-xl bg-muted/50 p-3 text-xs leading-5 text-muted-foreground">
              {pick({
                tr: "ODTÜ hesabını şimdilik bağlamadın. Bu adımı daha sonra Ayarlar'dan tamamlayabilirsin.",
                en: "You skipped connecting your METU account for now. You can complete this later in Settings.",
              })}
            </p>
          ) : null}

          <div className="mt-5 flex flex-col gap-3">
            <div className="grid grid-cols-[auto_1fr] gap-3 rounded-xl border border-primary/20 bg-primary/[0.035] p-4">
              <span className="grid size-9 place-items-center rounded-lg bg-primary/10 text-primary"><BookOpenCheckIcon className="size-4" /></span>
              <div><p className="text-sm font-semibold">{pick({ tr: "Ders programı ve akademik kayıt", en: "Schedule and academic record" })}</p><p className="mt-1 text-xs leading-5 text-muted-foreground">{pick({ tr: "Program, transkript, not ortalaması ve ders kataloğu.", en: "Schedule, transcript, GPA, and course catalog." })}</p></div>
            </div>
            <DataAccessChoice id="onboarding-odtuclass" icon={GraduationCapIcon} title="ODTÜClass" description={pick({ tr: "Kayıtlı dersler, duyurular, izlenceler ve teslim tarihleri.", en: "Enrolled courses, announcements, syllabi, and deadlines." })} detail={pick({ tr: "İzin vermediğin sürece ODTÜClass bilgilerine erişilmez.", en: "ODTÜClass is not accessed unless you allow it." })} checked={privacy.odtuclass} disabled={busy || !connection?.connected} optionalLabel={pick({ tr: "İsteğe bağlı", en: "Optional" })} onCheckedChange={(checked) => setPrivacyDraft({ ...privacy, odtuclass: checked })} />
            <DataAccessChoice id="onboarding-webmail" icon={MailIcon} title={pick({ tr: "ODTÜ e-postası", en: "METU email" })} description={pick({ tr: "E-postaları okuma ve arama; ileti gönderme veya yanıtlama.", en: "Read and search email; send or reply to messages." })} detail={pick({ tr: "E-posta göndermeden veya yanıtlamadan önce her zaman onayını ister.", en: "Always asks for your confirmation before sending or replying to an email." })} checked={privacy.webmail} disabled={busy || !connection?.connected} optionalLabel={pick({ tr: "İsteğe bağlı", en: "Optional" })} onCheckedChange={(checked) => setPrivacyDraft({ ...privacy, webmail: checked })} />
          </div>

          {formError ? <p className="mt-3 text-sm text-destructive">{formError}</p> : null}

          <div className="mt-6 flex items-center justify-between gap-3">
            <Button type="button" variant="ghost" size="sm" onClick={() => goTo("connect")} disabled={busy}>
              <ArrowLeftIcon /> {pick({ tr: "Geri", en: "Back" })}
            </Button>
            <Button type="button" onClick={() => void savePrivacyChoices()} disabled={busy}>
              {connect.isPending ? <Loader2Icon className="animate-spin" /> : null}
              {pick({ tr: "Devam et", en: "Continue" })} <ArrowRightIcon />
            </Button>
          </div>
        </section>
      ) : null}

      {step === "ready" ? (
        <section className="flex flex-1 flex-col">
          <div className="flex items-center gap-2 text-primary">
            <CheckCircle2Icon className="size-5" />
            <span className="text-xs font-semibold tracking-[0.18em] uppercase">
              {pick({ tr: "Hazırsın", en: "All set" })}
            </span>
          </div>
          <h1 className="mt-2 text-2xl font-semibold tracking-tight">
            {displayName.trim()
              ? pick({ tr: `Hadi başlayalım, ${displayName.trim()}`, en: `Let's get started, ${displayName.trim()}` })
              : pick({ tr: "Hadi başlayalım", en: "Let's get started" })}
          </h1>

          <PrivacySummary privacy={privacy} connected={Boolean(connection?.connected)} />

          <p className="mt-6 text-sm font-medium">{pick({ tr: "Şunları deneyebilirsin:", en: "Try asking:" })}</p>
          <ul className="mt-2 flex flex-col gap-1.5">
            {STARTER_PROMPTS[locale].map((prompt) => (
              <li key={prompt} className="rounded-lg bg-muted/50 px-3 py-2 text-sm text-muted-foreground">
                {prompt}
              </li>
            ))}
          </ul>

          {formError ? <p className="mt-3 text-sm text-destructive">{formError}</p> : null}

          <div className="mt-6 flex items-center justify-between gap-3">
            <Button type="button" variant="ghost" size="sm" onClick={() => goTo("privacy")} disabled={busy}>
              <ArrowLeftIcon /> {pick({ tr: "Geri", en: "Back" })}
            </Button>
            <Button type="button" onClick={() => void finish()} disabled={busy}>
              {update.isPending ? <Loader2Icon className="animate-spin" /> : null}
              {pick({ tr: "Sohbete başla", en: "Start chatting" })}
            </Button>
          </div>
        </section>
      ) : null}
    </div>
  );
}

function PrivacySummary({
  privacy,
  connected,
}: {
  privacy: PrivacyChoices;
  connected: boolean;
}) {
  const { pick } = useLocale();

  if (!connected) {
    return (
      <p className="mt-3 text-sm leading-6 text-muted-foreground">
        {pick({
          tr: "Asistanın genel bilgiyle çalışmaya hazır. ODTÜ bağlantısını ve isteğe bağlı veri erişimlerini daha sonra Ayarlar'dan ekleyebilirsin.",
          en: "Your assistant is ready to work from general knowledge. You can add your METU connection and optional data access later in Settings.",
        })}
      </p>
    );
  }

  return (
    <>
      <p className="mt-3 text-sm leading-6 text-muted-foreground">
        {pick({
          tr: "Bağlantın hazır. İzin verdiğin bilgi alanları:",
          en: "Your connection is ready. Information you allowed:",
        })}
      </p>
      <ul className="mt-3 flex flex-wrap gap-2">
        <li className="inline-flex items-center gap-1.5 rounded-lg border border-primary/20 bg-primary/5 px-2.5 py-1.5 text-xs font-medium"><BookOpenCheckIcon className="size-3.5 text-primary" />{pick({ tr: "Akademik kayıt", en: "Academic record" })}</li>
        {privacy.odtuclass ? <li className="inline-flex items-center gap-1.5 rounded-lg border border-primary/20 bg-primary/5 px-2.5 py-1.5 text-xs font-medium"><GraduationCapIcon className="size-3.5 text-primary" />ODTÜClass</li> : null}
        {privacy.webmail ? <li className="inline-flex items-center gap-1.5 rounded-lg border border-primary/20 bg-primary/5 px-2.5 py-1.5 text-xs font-medium"><MailIcon className="size-3.5 text-primary" />{pick({ tr: "ODTÜ e-postası", en: "METU email" })}</li> : null}
      </ul>
    </>
  );
}

function Footer({
  onNext,
  nextLabel,
  onSkip,
  skipLabel,
  busy,
}: {
  onNext: () => void;
  nextLabel: string;
  onSkip: () => void;
  skipLabel: string;
  busy: boolean;
}) {
  return (
    <div className="mt-7 flex items-center justify-between gap-3">
      <Button type="button" variant="ghost" size="sm" onClick={onSkip} disabled={busy}>
        {skipLabel}
      </Button>
      <Button type="button" onClick={onNext} disabled={busy}>
        {nextLabel} <ArrowRightIcon />
      </Button>
    </div>
  );
}
