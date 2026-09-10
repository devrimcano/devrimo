import type { Metadata } from "next";
import Link from "next/link";
import { ConsentPreference } from "@/components/consent-preference";

export const metadata: Metadata = {
  title: "Çerez ve Gizlilik Aydınlatma Metni · Devrimo",
  description: "Devrimo'nun hangi çerezleri neden kullandığı ve KVKK kapsamındaki haklarınız.",
};

/**
 * The notice the banner links to.
 *
 * Written in Turkish and not translated. KVKK's disclosure duty is owed to
 * people in Turkey and is discharged in Turkish; a second legal text in another
 * language is a second thing to keep accurate, and the day the two disagree the
 * question of which one governs is a worse problem than not having had it.
 *
 * The cookie table below is the actual inventory, measured in a browser against
 * the deployed site rather than transcribed from a vendor's documentation.
 */

/**
 * Who is answerable for this. Supplied by configuration rather than hardcoded
 * because it is the one part of this notice that source control is the wrong
 * place to decide, and because a deployment run by someone else must not claim
 * to be run by us.
 */
const CONTROLLER = {
  name: process.env.NEXT_PUBLIC_CONTROLLER_NAME?.trim() || "",
  contact: process.env.NEXT_PUBLIC_CONTROLLER_CONTACT?.trim() || "",
};

const COOKIES = [
  {
    name: "sb-…-auth-token",
    party: "Supabase (birinci taraf çerez)",
    purpose: "Oturumunuzu açık tutar. Bu çerez olmadan giriş yapılamaz.",
    kind: "Zorunlu",
    life: "Oturum boyunca / yenilenene kadar",
  },
  {
    name: "devrimo-cerez-tercihi",
    party: "Devrimo",
    purpose: "Bu sayfadaki çerez tercihinizi hatırlar; olmazsa her ziyarette tekrar sorulur.",
    kind: "Zorunlu",
    life: "12 ay",
  },
  {
    name: "devrimo-theme, devrimo-locale",
    party: "Devrimo",
    purpose: "Seçtiğiniz tema ve dil. Tarayıcınızın kendi deposunda tutulur, sunucuya gönderilmez.",
    kind: "Zorunlu",
    life: "Siz silene kadar",
  },
  {
    name: "ph_… (PostHog)",
    party: "PostHog (EU bulut)",
    purpose:
      "Kullanım ölçümü ve hata takibi: hangi sayfaların kullanıldığını sayar, çöken ekranları yakalar.",
    kind: "İsteğe bağlı — yalnızca açık rızanızla",
    life: "12 ay",
  },
  {
    name: "ph_… (PostHog, oturum kaydı)",
    party: "PostHog (EU bulut)",
    purpose:
      "Oturum kaydı: ekranınızdaki hareketleri kaydeder. Parola alanları kayıtta maskelenir. Ayrı olarak açılıp kapatılabilir.",
    kind: "İsteğe bağlı — yalnızca açık rızanızla",
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
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-8 px-4 py-10">
      <header className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold">Çerez ve Gizlilik Aydınlatma Metni</h1>
        <p className="text-muted-foreground text-sm leading-6">
          Bu metin, 6698 sayılı Kişisel Verilerin Korunması Kanunu&apos;nun 10. maddesi ve Kişisel Verileri Koruma
          Kurumu&apos;nun Çerez Uygulamaları Hakkında Rehberi kapsamında hazırlanmıştır.
        </p>
        <p className="text-muted-foreground text-xs">
          This notice is provided in Turkish, which is the governing text.
        </p>
      </header>

      <Section title="Veri sorumlusu">
        {CONTROLLER.name && CONTROLLER.contact ? (
          <p>
            {CONTROLLER.name} — {CONTROLLER.contact}
          </p>
        ) : (
          <p className="text-danger">
            Veri sorumlusunun kimlik ve iletişim bilgileri bu kurulumda tanımlanmamıştır.
          </p>
        )}
      </Section>

      <Section title="Hangi çerezleri kullanıyoruz">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[36rem] border-collapse text-left text-sm">
            <thead className="text-foreground">
              <tr className="border-border border-b">
                <th className="py-2 pr-3 font-medium">Çerez</th>
                <th className="py-2 pr-3 font-medium">Kaynak</th>
                <th className="py-2 pr-3 font-medium">Amaç</th>
                <th className="py-2 pr-3 font-medium">Tür</th>
                <th className="py-2 font-medium">Süre</th>
              </tr>
            </thead>
            <tbody>
              {COOKIES.map((cookie) => (
                <tr key={cookie.name} className="border-border/60 border-b align-top">
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
        <p>
          <strong className="text-foreground">Zorunlu çerezler kapatılamaz.</strong> Bunlar hizmetin
          sunulabilmesi için gereklidir — oturumunuzu açık tutan çerez olmadan giriş yapılamaz — ve açık rızaya değil,
          Kanun&apos;un 5/2-(c) ve 5/2-(f) bentlerindeki hukuki sebeplere dayanır. Bu nedenle size bir tercih olarak
          sunulmazlar; yalnızca burada olduğu gibi açıklanırlar.
        </p>
        <p>
          İsteğe bağlı çerezler ise tamamen size bağlıdır ve <strong className="text-foreground">ayrı ayrı</strong>{" "}
          açılıp kapatılabilir: kullanım ölçümünü kabul edip oturum kaydını reddedebilirsiniz. Hiçbiri siz izin
          vermeden yüklenmez; hepsini reddederseniz site tam olarak çalışmaya devam eder.
        </p>
      </Section>

      <Section title="Tercihinizi değiştirme">
        <p>
          Rızanızı istediğiniz an, verdiğiniz kolaylıkta geri alabilirsiniz. Geri aldığınızda ölçüm durdurulur ve
          tarayıcınızda bırakılmış ölçüm çerezleri silinir.
        </p>
        <ConsentPreference />
      </Section>

      <Section title="Yurt dışına aktarım">
        <p>
          İsteğe bağlı ölçüm çerezlerini kabul etmeniz hâlinde, bu veriler PostHog&apos;un Avrupa Birliği&apos;ndeki
          sunucularında işlenir. Bu, Kanun&apos;un 9. maddesi anlamında yurt dışına aktarımdır ve açık rızanıza
          dayanır. Kabul etmezseniz bu aktarım hiç gerçekleşmez.
        </p>
      </Section>

      <Section title="Haklarınız">
        <p>
          Kanun&apos;un 11. maddesi uyarınca; kişisel verilerinizin işlenip işlenmediğini öğrenme, işlenmişse buna
          ilişkin bilgi talep etme, işlenme amacını ve amacına uygun kullanılıp kullanılmadığını öğrenme, eksik veya
          yanlış işlenmişse düzeltilmesini, şartları oluştuğunda silinmesini veya yok edilmesini isteme, bu işlemlerin
          verilerin aktarıldığı üçüncü kişilere bildirilmesini isteme, işlenen verilerin münhasıran otomatik
          sistemlerle analiz edilmesi suretiyle aleyhinize bir sonuç ortaya çıkmasına itiraz etme ve verilerinizin
          kanuna aykırı işlenmesi sebebiyle zarara uğramanız hâlinde zararın giderilmesini talep etme haklarına
          sahipsiniz.
        </p>
        <p>
          Bu haklarınızı kullanmak için yukarıdaki iletişim adresine başvurabilirsiniz. Başvurular en geç otuz gün
          içinde sonuçlandırılır.
        </p>
      </Section>

      <footer className="text-muted-foreground text-sm">
        <Link href="/login" className="text-primary underline underline-offset-4">
          Girişe dön
        </Link>
      </footer>
    </main>
  );
}
