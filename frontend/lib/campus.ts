/**
 * Static fallbacks for campus copy.
 *
 * The authoritative tool list now comes from the broker
 * (`GET /api/campus/connection` → `tools`), because only the broker knows
 * which servers a student actually has credentials for. What's left here is
 * the copy that doesn't depend on that: prompts to seed an empty thread.
 */
export const STARTER_PROMPTS = [
  "When is the add/drop deadline this semester?",
  "Summarize this week's ODTÜClass announcements",
  "What is my CGPA from my transcript?",
  "List my upcoming assignment deadlines",
] as const;

export const AVAILABLE_TERMS = [
  { value: "20261", tr: "2026-2027 Güz", en: "2026-2027 Fall" },
  { value: "20262", tr: "2026-2027 Bahar", en: "2026-2027 Spring" },
  { value: "20253", tr: "2026 Yaz", en: "2026 Summer" },
] as const;

export function formatAcademicTerm(term: string, locale: "tr" | "en" = "tr"): string {
  const match = term.match(/^(\d{4})([1-3])$/);
  if (!match) return term;
  const year = parseInt(match[1], 10);
  const termNum = match[2];
  if (termNum === "1") {
    return locale === "tr" ? `${year}-${year + 1} Güz` : `${year}-${year + 1} Fall`;
  }
  if (termNum === "2") {
    return locale === "tr" ? `${year}-${year + 1} Bahar` : `${year}-${year + 1} Spring`;
  }
  if (termNum === "3") {
    return locale === "tr" ? `${year + 1} Yaz` : `${year + 1} Summer`;
  }
  return term;
}

export function formatUpdateCategory(type: string, locale: "tr" | "en" = "tr"): string {
  const normalized = type.toLowerCase().replace(/[^a-z0-9_]/g, "");
  const labels: Record<string, { tr: string; en: string }> = {
    academic_calendar: { tr: "Akademik Takvim", en: "Academic Calendar" },
    calendar: { tr: "Takvim", en: "Calendar" },
    announcement: { tr: "Duyuru", en: "Announcement" },
    event: { tr: "Etkinlik", en: "Event" },
    news: { tr: "Haber", en: "News" },
    mail_fact: { tr: "E-posta", en: "Email" },
    service_status: { tr: "Hizmet Bilgisi", en: "Service Status" },
    servicestatus: { tr: "Hizmet Bilgisi", en: "Service Status" },
    working_hours: { tr: "Çalışma Saatleri", en: "Opening Hours" },
    workinghours: { tr: "Çalışma Saatleri", en: "Opening Hours" },
  };
  return labels[normalized]?.[locale] ?? type.replaceAll("_", " ");
}

export function formatUpdateSource(source: string): string {
  const sources: Record<string, string> = {
    odtuclass_announcements: "ODTÜClass",
    odtuclass: "ODTÜClass",
    academic_calendar: "Akademik Takvim",
    sais: "Öğrenci İşleri",
  };
  return sources[source] ?? source.replaceAll("_", " ");
}

