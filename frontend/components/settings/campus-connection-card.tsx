"use client";

import { useState } from "react";
import { toast } from "sonner";
import {
  BookOpenCheckIcon,
  CableIcon,
  CheckCircle2Icon,
  GraduationCapIcon,
  Loader2Icon,
  LockIcon,
  MailIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { useLocale } from "@/components/locale-provider";
import { useCampus } from "@/hooks/useCampus";
import { captureError, captureProductEvent } from "@/components/posthog-analytics";
import { DataAccessChoice } from "@/components/settings/data-access-choice";

const CORE_ACCESS = ["sais", "course_info"];

type PrivacyChoices = {
  odtuclass: boolean;
  webmail: boolean;
};

export function CampusConnectionCard() {
  const { pick, locale } = useLocale();
  const { connection, isLoading, connect, disconnect, apply } = useCampus();
  const [usernameDraft, setUsernameDraft] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [privacyDraft, setPrivacyDraft] = useState<PrivacyChoices | null>(null);

  const username = usernameDraft ?? connection?.metu_username ?? "";
  const savedPrivacy: PrivacyChoices = {
    odtuclass: connection?.enabled_tools.includes("odtuclass") ?? false,
    webmail: connection?.enabled_tools.includes("webmail") ?? false,
  };
  const privacy = privacyDraft ?? savedPrivacy;
  const privacyDirty =
    privacyDraft !== null &&
    (privacy.odtuclass !== savedPrivacy.odtuclass || privacy.webmail !== savedPrivacy.webmail);
  const usernameDirty = usernameDraft !== null && username.trim() !== (connection?.metu_username ?? "");
  const busy = connect.isPending || disconnect.isPending || apply.isPending;

  async function save() {
    if (!username.trim()) {
      toast.error(pick({ tr: "ODTÜ kullanıcı adı gerekli.", en: "A METU username is required." }));
      return;
    }

    const enabledAccess = [
      ...CORE_ACCESS,
      ...(privacy.odtuclass ? ["odtuclass"] : []),
      ...(privacy.webmail ? ["webmail"] : []),
    ];

    try {
      const result = await connect.mutateAsync({
        metu_username: username.trim(),
        ...(password ? { metu_password: password } : {}),
        locale,
        enabled_tools: enabledAccess,
      });
      if (result.needs_restart) await apply.mutateAsync();
      setPassword("");
      setPrivacyDraft(null);
      setUsernameDraft(null);
      captureProductEvent("campus_connection_saved", {
        source: "settings",
        result: "success",
        verification_skipped: !result.verified_at,
      });
      captureProductEvent("campus_tools_changed", {
        source: "settings",
        tool_count: enabledAccess.length,
        result: "success",
      });
      toast.success(
        result.verified_at
          ? pick({ tr: "ODTÜ bağlantın ve gizlilik seçimlerin güncellendi.", en: "Your METU connection and privacy choices are updated." })
          : pick({ tr: "Kaydedildi, ancak ODTÜ girişi doğrulanamadı.", en: "Saved, but the METU sign-in could not be verified." }),
      );
    } catch (error) {
      captureProductEvent("campus_connection_saved", {
        source: "settings",
        result: "error",
        verification_skipped: false,
      });
      captureError(error, { source: "settings_campus_save" });
      toast.error(error instanceof Error ? error.message : pick({ tr: "Kaydedilemedi.", en: "Could not save." }));
    }
  }

  async function removeConnection() {
    try {
      await disconnect.mutateAsync();
      setPrivacyDraft(null);
      setUsernameDraft(null);
      setPassword("");
      captureProductEvent("campus_disconnected", { result: "success" });
      toast.success(pick({ tr: "ODTÜ bağlantın kaldırıldı.", en: "Your METU account is disconnected." }));
    } catch (error) {
      captureProductEvent("campus_disconnected", { result: "error" });
      captureError(error, { source: "settings_campus_action" });
      toast.error(error instanceof Error ? error.message : pick({ tr: "İşlem başarısız oldu.", en: "Action failed." }));
    }
  }

  return (
    <Card id="connection" className="surface-raised scroll-mt-32 border-0 ring-1 ring-foreground/8 lg:scroll-mt-24">
      <CardHeader className="border-b bg-muted/20">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-start gap-3">
            <span className="grid size-9 shrink-0 place-items-center rounded-xl bg-primary/10 text-primary"><CableIcon className="size-4" /></span>
            <div>
              <CardTitle>{pick({ tr: "ODTÜ hesabı", en: "METU account" })}</CardTitle>
              <CardDescription className="mt-1 max-w-2xl">
                {pick({ tr: "Ders programın ve akademik kayıtların için hesabını bağla. ODTÜClass ve e-posta erişimi ayrıca ve isteğe bağlı olarak seçilir.", en: "Connect your account for your schedule and academic record. ODTÜClass and email access are separate, optional choices." })}
              </CardDescription>
            </div>
          </div>
          {connection?.connected ? (
            <Badge variant={connection.verified_at ? "secondary" : "outline"} className="gap-1.5">
              {connection.verified_at ? <CheckCircle2Icon /> : <TriangleAlertIcon />}
              {connection.verified_at ? pick({ tr: "Bağlı", en: "Connected" }) : pick({ tr: "Doğrulama gerekli", en: "Needs verification" })}
            </Badge>
          ) : null}
        </div>
      </CardHeader>

      <CardContent className="space-y-7">
        {isLoading ? (
          <div className="flex min-h-32 items-center justify-center gap-2 text-sm text-muted-foreground">
            <Loader2Icon className="size-4 animate-spin" />
            {pick({ tr: "Bağlantı bilgileri yükleniyor…", en: "Loading connection details…" })}
          </div>
        ) : (
          <>
            {connection?.verification_error ? (
              <div className="flex items-start gap-2 rounded-xl border border-amber-500/20 bg-amber-500/8 p-3 text-sm text-amber-800 dark:text-amber-300">
                <TriangleAlertIcon className="mt-0.5 size-4 shrink-0" />
                <span>{connection.verification_error}</span>
              </div>
            ) : null}

            <section aria-labelledby="credentials-heading" className="space-y-4">
              <div>
                <h2 id="credentials-heading" className="text-sm font-semibold">{pick({ tr: "Bağlantı bilgileri", en: "Connection details" })}</h2>
                <p className="mt-1 text-xs text-muted-foreground">{pick({ tr: "Şifren güvenli şekilde saklanır ve kimseyle paylaşılmaz.", en: "Your password is stored securely and never shared." })}</p>
              </div>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="settings-metu-username">{pick({ tr: "ODTÜ kullanıcı adı", en: "METU username" })}</Label>
                  <Input id="settings-metu-username" value={username} spellCheck={false} autoComplete="username" placeholder="e123456" onChange={(event) => setUsernameDraft(event.target.value)} />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="settings-metu-password">{pick({ tr: "ODTÜ şifresi", en: "METU password" })}</Label>
                  <Input id="settings-metu-password" type="password" value={password} autoComplete="current-password" placeholder={connection?.has_password ? pick({ tr: "Kayıtlı — değiştirmek için yaz", en: "Stored — type to replace" }) : "••••••••"} onChange={(event) => setPassword(event.target.value)} />
                </div>
              </div>
            </section>

            <section id="privacy" aria-labelledby="privacy-heading" className="scroll-mt-32 space-y-3 lg:scroll-mt-24">
              <div>
                <h2 id="privacy-heading" className="text-sm font-semibold">{pick({ tr: "Veri erişimi", en: "Data access" })}</h2>
                <p className="mt-1 text-xs leading-5 text-muted-foreground">{pick({ tr: "Yalnızca kullanmak istediğin bilgi kaynaklarına izin ver. Bu seçimler istediğin zaman değiştirilebilir.", en: "Allow only the information sources you want to use. You can change these choices at any time." })}</p>
              </div>

              <div className="grid grid-cols-[auto_1fr] gap-3 rounded-xl border border-primary/20 bg-primary/[0.035] p-4">
                <span className="grid size-9 place-items-center rounded-lg bg-primary/10 text-primary"><BookOpenCheckIcon className="size-4" /></span>
                <div>
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="text-sm font-semibold">{pick({ tr: "Ders programı ve akademik kayıt", en: "Schedule and academic record" })}</p>
                    <Badge variant="secondary" className="text-[10px]">{pick({ tr: "Bağlantıya dahil", en: "Included" })}</Badge>
                  </div>
              <p className="mt-1 text-sm leading-5 text-muted-foreground">{pick({ tr: "Program, transkript ve ders kataloğu bilgileri.", en: "Schedule, transcript, and course catalog information." })}</p>
                </div>
              </div>

              <DataAccessChoice id="allow-odtuclass" icon={GraduationCapIcon} title="ODTÜClass" description={pick({ tr: "Derslerin, duyuruların ve yaklaşan teslim tarihlerin.", en: "Your courses, announcements, and upcoming deadlines." })} detail={pick({ tr: "Yalnızca izin verdiğinde ODTÜClass bilgilerin kullanılır.", en: "ODTÜClass information is accessed only when enabled." })} checked={privacy.odtuclass} disabled={busy} optionalLabel={pick({ tr: "İsteğe bağlı", en: "Optional" })} onCheckedChange={(checked) => setPrivacyDraft({ ...privacy, odtuclass: checked })} />

              <DataAccessChoice id="allow-webmail" icon={MailIcon} title={pick({ tr: "ODTÜ e-postası", en: "METU email" })} description={pick({ tr: "E-postalarını okuma ve arama.", en: "Read and search your email." })} detail={pick({ tr: "Göndermeden veya yanıtlamadan önce her zaman onayını ister.", en: "Always asks for your confirmation before sending or replying." })} checked={privacy.webmail} disabled={busy} optionalLabel={pick({ tr: "İsteğe bağlı", en: "Optional" })} onCheckedChange={(checked) => setPrivacyDraft({ ...privacy, webmail: checked })} />
            </section>

            <div className="flex flex-wrap items-center gap-2 border-t pt-5">
              <Button className="disabled:bg-muted disabled:text-muted-foreground disabled:opacity-100" disabled={busy || (Boolean(connection?.connected) && !password && !privacyDirty && !usernameDirty)} onClick={() => void save()}>
                {busy ? <Loader2Icon className="animate-spin" /> : null}
                {connection?.connected ? pick({ tr: "ODTÜ bağlantısını kaydet", en: "Save METU connection" }) : pick({ tr: "Hesabı bağla", en: "Connect account" })}
              </Button>

              {connection?.connected ? (
                <AlertDialog>
                  <AlertDialogTrigger render={<Button variant="outline" disabled={busy} />}>{pick({ tr: "Bağlantıyı kaldır", en: "Disconnect" })}</AlertDialogTrigger>
                  <AlertDialogContent>
                    <AlertDialogHeader>
                      <AlertDialogTitle>{pick({ tr: "ODTÜ bağlantısı kaldırılsın mı?", en: "Disconnect your METU account?" })}</AlertDialogTitle>
                      <AlertDialogDescription>{pick({ tr: "Kayıtlı ODTÜ şifren silinir ve asistanın ODTÜ sistemlerine erişimi kaldırılır. Sohbet geçmişin ve hatırlanan tercihlerin kalır.", en: "Your stored METU password is deleted and the assistant loses access to METU systems. Chat history and remembered preferences remain." })}</AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                      <AlertDialogCancel>{pick({ tr: "Vazgeç", en: "Cancel" })}</AlertDialogCancel>
                      <AlertDialogAction onClick={() => void removeConnection()}>{pick({ tr: "Bağlantıyı kaldır", en: "Disconnect" })}</AlertDialogAction>
                    </AlertDialogFooter>
                  </AlertDialogContent>
                </AlertDialog>
              ) : null}
            </div>

            <p className="flex items-start gap-2 text-[11px] leading-4 text-muted-foreground">
              <LockIcon className="mt-0.5 size-3.5 shrink-0" />
              <span>{pick({ tr: "Bağlantın ve verilerin yalnızca sana özeldir.", en: "Your connection and data are private to you." })}</span>
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
