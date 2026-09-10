import type { Metadata } from "next";
import Link from "next/link";
import { ConsentPreference } from "@/components/consent-preference";

export const metadata: Metadata = {
  title: "Çerezler ve gizlilik · Devrimo",
  description: "Devrimo hangi çerezleri neden kullanıyor, ve bunu istediğin an nasıl değiştirirsin.",
};

/**
 * The notice the banner links to, written to be read.
 *
 * The first version was legally complete and nobody would have read it: it
 * opened with a statute number, mixed "sen" and "siz" in the same paragraph,
 * and put a red warning above the part that answers the actual question. A
 * notice that intimidates the reader informs nobody, which is the opposite of
 * what a disclosure duty is for.
 *
 * So it is layered, which is the shape the KVKK guidance itself favours: the
 * plain answer and the switch that acts on it come first, the cookie inventory
 * next, and the statutory detail last, folded away — still complete, still
 * exact, just not the first thing a student meets.
 *
 * Written in Turkish and not translated: the disclosure is owed to people in
 * Turkey and is discharged in Turkish, and a second legal text in another
 * language is a second thing to keep true.
 */

/**
 * Who is answerable for this. Supplied by configuration rather than hardcoded,
 * because a deployment run by someone else must not claim to be run by us.
 */
const CONTROLLER = {
  name: process.env.NEXT_PUBLIC_CONTROLLER_NAME?.trim() || "",
  contact: process.env.NEXT_PUBLIC_CONTROLLER_CONTACT?.trim() || "",
};

const COOKIES = [
  {
    name: "sb-…-auth-token",
    party: "Supabase",
    purpose: "Girişini açık tutar. Bu olmadan her sayfada yeniden giriş yapman gerekirdi.",
    kind: "Şart",
    life: "Çıkış yapana kadar",
  },
  {
    name: "devrimo-cerez-tercihi",
    party: "Devrimo",
    purpose: "Bu sayfadaki cevabını hatırlar. Olmasa her açtığında aynı soruyu sorardık.",
    kind: "Şart",
    life: "12 ay",
  },
  {
    name: "devrimo-theme, devrimo-locale",
    party: "Devrimo",
    purpose: "Seçtiğin tema ve dil. Tarayıcında kalır, bize hiç gelmez.",
    kind: "Şart",
    life: "Sen silene kadar",
  },
  {
    name: "ph_… (PostHog)",
    party: "PostHog · AB sunucuları",
    purpose: "Hangi sayfaların işine yaradığını sayar, bir ekran çöktüğünde bize haber verir.",
    kind: "Senin izninle",
    life: "12 ay",
  },
  {
    name: "ph_… (PostHog)",
    party: "PostHog · AB sunucuları",
    purpose: "Bir şey bozulduğunda ne olduğunu geri sarıp görebilmemiz için. Yazdığın şifreler kayda girmez.",
    kind: "Senin izninle · ayrı sorulur",
    life: "12 ay",
  },
];

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-base font-semibold">{title}</h2>
      <div className="text-muted-foreground flex flex-col gap-2 text-sm leading-6">{children}</div>
    </section>
  );
}

export default function PrivacyPage() {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-col gap-8 px-4 py-10">
      <header className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold">Çerezler ve gizlilik</h1>
        <p className="text-muted-foreground text-sm leading-6">
          Kısa versiyonu: Devrimo&apos;yu çalıştıran birkaç çerez var, onlar hep açık. Başka hiçbir şey sen izin
          vermeden yüklenmiyor.
        </p>
      </header>

      {/* The answer and the switch, before anything a reader has to wade through. */}
      <section className="border-border bg-card/60 flex flex-col gap-4 rounded-xl border p-4">
        <ul className="text-muted-foreground flex flex-col gap-2 text-sm leading-6">
          <li>
            <strong className="text-foreground">Şart olanlar:</strong> girişini hatırlamak, temanı ve dilini bilmek,
            bir de bu sayfadaki cevabını saklamak. Bunlar kapatılamıyor, çünkü kapatılırsa site çalışmıyor.
          </li>
          <li>
            <strong className="text-foreground">İsteğe bağlı olanlar:</strong> neyin kullanıldığını saymak ve bir şey
            bozulduğunda haberimizin olması. Bunlar tamamen sana bağlı.
          </li>
          <li>
            <strong className="text-foreground">Reddedersen ne olur:</strong> hiçbir şey. Site aynı şekilde çalışır,
            hiçbir özellik kapanmaz.
          </li>
          <li>Fikrini istediğin an değiştirebilirsin — aşağıdaki düğmeler her zaman burada.</li>
        </ul>

        <ConsentPreference />
      </section>

      <Section title="Tam liste">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[34rem] border-collapse text-left text-sm">
            <thead className="text-foreground">
              <tr className="border-border border-b">
                <th className="py-2 pr-3 font-medium">Çerez</th>
                <th className="py-2 pr-3 font-medium">Kaynak</th>
                <th className="py-2 pr-3 font-medium">Ne işe yarıyor</th>
                <th className="py-2 pr-3 font-medium">Tür</th>
                <th className="py-2 font-medium">Süre</th>
              </tr>
            </thead>
            <tbody>
              {COOKIES.map((cookie) => (
                <tr key={`${cookie.name}-${cookie.purpose}`} className="border-border/60 border-b align-top">
                  <td className="py-2 pr-3 font-mono text-xs">{cookie.name}</td>
                  <td className="py-2 pr-3">{cookie.party}</td>
                  <td className="py-2 pr-3">{cookie.purpose}</td>
                  <td className="py-2 pr-3">{cookie.kind}</td>
                  <td className="py-2">{cookie.life}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>

      <Section title="İzin verirsen veriler nereye gidiyor">
        <p>
          Ölçüm verileri PostHog&apos;un Avrupa Birliği&apos;ndeki sunucularında tutuluyor. İzin vermezsen bu aktarım
          hiç olmuyor — veri de olmuyor.
        </p>
      </Section>

      {/* Complete and exact, and out of the way. Someone who needs the article
          numbers is looking for them; nobody else should have to read past
          them to find the switch. */}
      <details className="border-border rounded-xl border p-4">
        <summary className="cursor-pointer text-sm font-medium">
          KVKK kapsamındaki hukuki ayrıntılar
        </summary>

        <div className="text-muted-foreground mt-4 flex flex-col gap-4 text-sm leading-6">
          <p>
            Bu metin, 6698 sayılı Kişisel Verilerin Korunması Kanunu&apos;nun 10. maddesindeki aydınlatma yükümlülüğü
            ve Kişisel Verileri Koruma Kurumu&apos;nun Çerez Uygulamaları Hakkında Rehberi kapsamında hazırlanmıştır.
          </p>

          <div>
            <h3 className="text-foreground font-medium">Veri sorumlusu</h3>
            {CONTROLLER.name && CONTROLLER.contact ? (
              <p>
                {CONTROLLER.name} — {CONTROLLER.contact}
              </p>
            ) : (
              <p>Veri sorumlusuna ait iletişim bilgileri bu kurulumda henüz tanımlanmamıştır.</p>
            )}
          </div>

          <div>
            <h3 className="text-foreground font-medium">Hukuki sebep</h3>
            <p>
              Zorunlu çerezler açık rızaya değil, Kanun&apos;un 5/2-(c) ve 5/2-(f) bentlerindeki hukuki sebeplere
              dayanır; bu nedenle bir tercih olarak sunulmaz, yalnızca açıklanır. İsteğe bağlı çerezler yalnızca açık
              rızanıza dayanır, kategori kategori ayrı ayrı verilebilir ve aynı kolaylıkta geri alınabilir. Geri
              aldığınızda ölçüm durur ve tarayıcınızda bırakılmış ölçüm çerezleri silinir.
            </p>
          </div>

          <div>
            <h3 className="text-foreground font-medium">Yurt dışına aktarım</h3>
            <p>
              İsteğe bağlı çerezleri kabul etmeniz hâlinde veriler PostHog&apos;un Avrupa Birliği&apos;ndeki
              sunucularında işlenir. Bu, Kanun&apos;un 9. maddesi anlamında yurt dışına aktarım sayılır ve açık
              rızanıza dayanır.
            </p>
          </div>

          <div>
            <h3 className="text-foreground font-medium">Haklarınız</h3>
            <p>
              Kanun&apos;un 11. maddesi uyarınca; kişisel verilerinizin işlenip işlenmediğini öğrenme, işlenmişse buna
              ilişkin bilgi talep etme, işlenme amacını ve amacına uygun kullanılıp kullanılmadığını öğrenme, eksik
              veya yanlış işlenmişse düzeltilmesini, şartları oluştuğunda silinmesini veya yok edilmesini isteme, bu
              işlemlerin verilerin aktarıldığı üçüncü kişilere bildirilmesini isteme, işlenen verilerin münhasıran
              otomatik sistemlerle analiz edilmesi suretiyle aleyhinize bir sonuç ortaya çıkmasına itiraz etme ve
              verilerinizin kanuna aykırı işlenmesi sebebiyle zarara uğramanız hâlinde zararın giderilmesini talep
              etme haklarına sahipsiniz. Başvurular en geç otuz gün içinde sonuçlandırılır.
            </p>
          </div>
        </div>
      </details>

      <footer className="text-muted-foreground text-sm">
        <Link href="/login" className="text-primary underline underline-offset-4">
          Girişe dön
        </Link>
      </footer>
    </main>
  );
}
