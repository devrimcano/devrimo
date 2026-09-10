/**
 * The legal texts, and which version of each one a person was shown.
 *
 * Two things this exists for, neither of which is the wording:
 *
 *   - **A version to point at.** KVKK puts the burden of proof on the data
 *     controller: it is not enough that a notice existed, it has to be provable
 *     that *this* person saw *that* text on *that* day. A consent record is
 *     meaningless without a stable identifier for what was consented to, so
 *     every document carries one, and it changes whenever the words change.
 *   - **A place for the words to land.** The bodies below are deliberately
 *     unwritten. Filling them is a lawyer's job, and a plausible-sounding
 *     legal text written by someone unqualified is worse than a blank one:
 *     blank is obviously unfinished, whereas plausible gets shipped.
 *
 * So the structure is real and the prose is not. `status: "draft"` is load
 * bearing — a draft renders with a banner saying so, is kept out of robots.txt,
 * and must never be used as the target of a recorded consent.
 */

export type LegalSection = {
  heading: string;
  /** Paragraphs. A `[[ ... ]]` span marks something only a lawyer can fill. */
  body: string[];
};

export type LegalDocument = {
  /** The URL segment: /belgeler/<slug>. */
  slug: string;
  /**
   * Stable across rewordings, unlike the version. A consent record stores this
   * plus the version, so "did they agree to the current text" is answerable.
   */
  id: string;
  /**
   * Bumped whenever a single word of the body changes. Date-ordered so the
   * sequence is readable without a lookup; the suffix distinguishes two
   * revisions on the same day.
   */
  version: string;
  effectiveFrom: string;
  title: string;
  summary: string;
  /**
   * "draft" until a lawyer has read it. A draft is shown with a banner, kept
   * out of robots.txt, and refused as a consent target.
   */
  status: "draft" | "published";
  sections: LegalSection[];
};

/** How an unwritten span is marked, so one regex finds them all. */
export const PLACEHOLDER = /\[\[(.+?)\]\]/g;

export const LEGAL_DOCUMENTS: readonly LegalDocument[] = [
  {
    slug: "aydinlatma",
    id: "kvkk.aydinlatma",
    version: "2026-09-10.1",
    effectiveFrom: "2026-09-10",
    title: "Aydınlatma Metni",
    summary:
      "Devrimo'nun hangi kişisel verini, neden ve hangi hukuki sebeple işlediği; kimlere aktarıldığı, ne kadar saklandığı ve haklarının neler olduğu.",
    status: "draft",
    sections: [
      {
        heading: "Veri sorumlusu",
        body: [
          "[[Veri sorumlusunun adı/unvanı, adresi ve başvuruların ulaşacağı iletişim adresi. Şahıs mı, şahıs firması mı olduğu netleştirilmeli.]]",
        ],
      },
      {
        heading: "İşlenen kişisel veriler",
        body: [
          "Devrimo'nun bugün fiilen işlediği veriler, kod okunarak çıkarılmıştır ve metin yazılırken bu liste esas alınmalıdır:",
          "Hesap bilgileri: e-posta adresi, görünen ad, dil tercihi. ODTÜ bağlantısı: ODTÜ kullanıcı adı ve şifresi (şifrelenmiş olarak saklanır, hiçbir yanıtta geri döndürülmez). Akademik bağlam: bölüm, sınıf, program kodu, kampüs, soyadının ilk iki harfi. Ders ve program verisi: planlanan haftalık program, SAIS'ten okunan kayıt ve transkript bilgisi. İletişim verisi: yalnızca sen izin verirsen ODTÜ webmail içeriği ve ODTÜClass duyuru/ödev bilgisi. Kullanım verisi: sohbet içeriği, hatırlanmasını açıkça istediğin tercihler.",
          "[[Yukarıdaki listenin hukuki kategorilere göre yeniden düzenlenmesi ve eksik kalan bir kalem varsa eklenmesi.]]",
        ],
      },
      {
        heading: "İşleme amaçları",
        body: [
          "[[Her veri kategorisi için amaç. Çekirdek amaç: öğrencinin talep ettiği asistan hizmetinin sunulması — ders planlama, kısıt kontrolü, kampüs sistemlerinden bilgi getirme.]]",
        ],
      },
      {
        heading: "Hukuki sebepler",
        body: [
          "Değerlendirmemiz, çekirdek işlemenin Kanun'un 5/2-(c) bendine — sözleşmenin kurulması veya ifasıyla doğrudan doğruya ilgili olması — dayandığı yönündedir. Bu kısım için açık rıza aranmaz ve aranmamalıdır: geri alınabilir bir rızaya dayandırılırsa, yerine getirilemeyecek bir durdurma yükümlülüğü doğar.",
          "İsteğe bağlı olan ve açık rızaya dayanan kısımlar ayrıdır: ODTÜ webmail ve ODTÜClass erişimi, ölçüm ve hata takibi çerezleri, oturum kaydı.",
          "[[Bu değerlendirmenin hukukçu tarafından teyidi; her kalem için nihai hukuki sebep.]]",
        ],
      },
      {
        heading: "Aktarım ve yurt dışına aktarım",
        body: [
          "Asistanın çalışması, sohbet içeriğinin ve ilgili bağlamın yurt dışındaki bir model sağlayıcısına aktarılmasını gerektirir. Bu, Kanun'un 9. maddesi anlamında yurt dışına aktarımdır ve işleme dayanağından ayrı bir mekanizma gerektirir.",
          "[[Aktarım mekanizmasının belirtilmesi: standart sözleşme imzalandıysa buna atıf, imza ve Kuruma bildirim tarihleriyle. Alıcı taraflar isim isim sayılmalı.]]",
        ],
      },
      {
        heading: "Saklama süresi",
        body: [
          "[[Her veri kategorisi için saklama süresi ve silme tetikleyicisi. Hesap silindiğinde nelerin ne kadar sürede yok edildiği.]]",
        ],
      },
      {
        heading: "Haklarınız",
        body: [
          "[[Kanun'un 11. maddesindeki haklar ve bunların nasıl kullanılacağı; başvuru kanalı ve otuz günlük yanıt süresi.]]",
        ],
      },
    ],
  },
  {
    slug: "acik-riza",
    id: "kvkk.acik-riza.kampus",
    version: "2026-09-10.1",
    effectiveFrom: "2026-09-10",
    title: "Açık Rıza Metni — Kampüs Erişimi",
    summary:
      "ODTÜ şifresinin saklanması ve senin adına kampüs sistemlerine bağlanılması için istenen açık rıza.",
    status: "draft",
    sections: [
      {
        heading: "Rızanın konusu",
        body: [
          "Bu metin yalnızca kampüs erişimini kapsar ve tek başına sorulur. Devrimo'nun geri kalanı bu rıza olmadan da çalışır: bağlanmayan bir öğrenci asistanı kullanmaya devam eder, yalnızca ODTÜ sistemlerinden bilgi getirilemez.",
          "[[Rızanın kapsamı: şifrenin şifrelenmiş olarak saklanması, öğrenci adına oturum açılması, hangi sistemlere hangi amaçla bağlanılacağı.]]",
        ],
      },
      {
        heading: "Neye rıza verilmiş olmuyor",
        body: [
          "Araçlar bir izin listesiyle sınırlıdır: silme, taşıma, işaretleme ve iletme araçları hiç açılmamıştır. Dışa dönük tek eylem olan e-posta gönderme ve yanıtlama, her seferinde tam metnin ayrıca onaylanmasına bağlıdır.",
          "[[Bu sınırların hukuki dille ifadesi.]]",
        ],
      },
      {
        heading: "Rızanın geri alınması",
        body: [
          "[[Geri alma yöntemi ve geri alındığında saklanan kimlik bilgilerine ne olduğu.]]",
        ],
      },
    ],
  },
];

export function findDocument(slug: string): LegalDocument | undefined {
  return LEGAL_DOCUMENTS.find((document) => document.slug === slug);
}

/** Every unwritten span in a document, for the draft banner's count. */
export function placeholderCount(document: LegalDocument): number {
  return document.sections.reduce(
    (total, section) =>
      total + section.body.reduce((count, paragraph) => count + [...paragraph.matchAll(PLACEHOLDER)].length, 0),
    0,
  );
}

/**
 * What a consent record should store, and a refusal to produce one for a draft.
 *
 * Recording consent against a text that has not been reviewed would put a
 * version number on a promise nobody has checked — the appearance of proof
 * rather than proof.
 */
export function consentTarget(document: LegalDocument): { id: string; version: string } {
  if (document.status !== "published") {
    throw new Error(`${document.id} is still a draft and cannot be the target of a recorded consent`);
  }
  return { id: document.id, version: document.version };
}
