/**
 * What the assistant just did, in a student's words.
 *
 * The agent's seven tools speak in resource kinds (`student.registered_schedule`,
 * `mail.messages`, `planning.timetable`). Those names are a contract between the
 * broker and the model; they were never copy. Rendering them raw is how a
 * transcript ended up reading `Used tool: read` above a JSON dump, in English,
 * inside a Turkish-default product.
 *
 * Two rules hold this file together:
 *
 * 1. **Provenance is the point.** The product's own rule is that campus data is
 *    never invented, so every line names which METU system answered — SAIS,
 *    ODTÜClass, webmail, the course catalog — or names Devrimo when the answer
 *    came from the student's own saved state. A kind we do not recognise gets a
 *    neutral phrase and no source, never a guessed one.
 * 2. **Nothing is capitalized by CSS.** `text-transform: capitalize` under
 *    `lang="tr"` turns an argument key beginning with `i` into `İ`, which is how
 *    the email confirmation dialog came to ask a student to approve
 *    "İmportance". Labels here are written out, in both languages.
 */

export type Copy = { tr: string; en: string };

/** A resource kind as the student's own description of it, plus who holds it. */
type ResourceCopy = { what: Copy; source?: Copy };

const SAIS: Copy = { tr: "SAIS", en: "SAIS" };
const ODTUCLASS: Copy = { tr: "ODTÜClass", en: "ODTÜClass" };
const WEBMAIL: Copy = { tr: "ODTÜ webmail", en: "METU webmail" };
const CATALOG: Copy = { tr: "ODTÜ ders kataloğu", en: "the METU course catalog" };
const DEVRIMO: Copy = { tr: "Devrimo", en: "Devrimo" };
const PLANNER: Copy = { tr: "ders programın", en: "your planner" };
const AVESIS: Copy = { tr: "AVESIS", en: "AVESIS" };

const RESOURCE_COPY: Record<string, ResourceCopy> = {
  "student.registered_schedule": { what: { tr: "Kayıtlı ders programın", en: "Your registered schedule" }, source: SAIS },
  "student.transcript": { what: { tr: "Transkriptin", en: "Your transcript" }, source: SAIS },
  "student.info": { what: { tr: "Öğrenci bilgilerin", en: "Your student record" }, source: SAIS },
  "student.announcements": { what: { tr: "Portal duyuruları", en: "Portal announcements" }, source: SAIS },
  "student.curriculum": { what: { tr: "Müfredat panon", en: "Your curriculum board" }, source: SAIS },
  "student.categories": { what: { tr: "Müfredat kategorilerin", en: "Your curriculum categories" }, source: SAIS },
  "student.category_courses": { what: { tr: "Kategorindeki dersler", en: "The courses in that category" }, source: SAIS },

  "catalog.department": { what: { tr: "Bölümün bu dönem açtığı dersler", en: "The department's courses this term" }, source: CATALOG },
  "catalog.departments": { what: { tr: "Bölüm listesi", en: "The department list" }, source: CATALOG },
  "catalog.courses": { what: { tr: "Ders bilgileri", en: "Course details" }, source: CATALOG },
  "catalog.sections": { what: { tr: "Şubeler", en: "Course sections" }, source: CATALOG },
  "catalog.eligibility": { what: { tr: "Şube kısıtları", en: "Section restrictions" }, source: CATALOG },
  "catalog.prerequisites": { what: { tr: "Ön koşullar", en: "Prerequisites" }, source: CATALOG },
  "catalog.replacements": { what: { tr: "Denk sayılan dersler", en: "Replacement courses" }, source: CATALOG },
  "catalog.theses": { what: { tr: "Tez dersleri", en: "Thesis courses" }, source: CATALOG },

  "class.courses": { what: { tr: "ODTÜClass derslerin", en: "Your ODTÜClass courses" }, source: ODTUCLASS },
  "class.announcements": { what: { tr: "Ders duyuruları", en: "Course announcements" }, source: ODTUCLASS },
  "class.syllabus": { what: { tr: "Ders izlencesi", en: "The course syllabus" }, source: ODTUCLASS },
  "class.assignments": { what: { tr: "Yaklaşan ödev teslimleri", en: "Upcoming assignments" }, source: ODTUCLASS },
  "class.labs": { what: { tr: "Lab ve recitation bilgisi", en: "Lab and recitation info" }, source: ODTUCLASS },

  "mail.status": { what: { tr: "Posta kutun", en: "Your mailbox" }, source: WEBMAIL },
  "mail.folders": { what: { tr: "Posta klasörlerin", en: "Your mail folders" }, source: WEBMAIL },
  "mail.messages": { what: { tr: "E-postaların", en: "Your email" }, source: WEBMAIL },
  "mail.message": { what: { tr: "Bir e-posta", en: "An email" }, source: WEBMAIL },
  "mail.attachment": { what: { tr: "E-posta eki", en: "An email attachment" }, source: WEBMAIL },

  "planning.timetable": { what: { tr: "Planlayıcıdaki haftan", en: "The week in your planner" }, source: PLANNER },
  "planning.proposal": { what: { tr: "Program önerisi", en: "A schedule proposal" }, source: PLANNER },
  "planning.course_group": { what: { tr: "Ders grubu", en: "A course group" }, source: PLANNER },

  "my.academic_snapshot": { what: { tr: "Kayıtlı akademik bilgilerin", en: "Your saved academic record" }, source: DEVRIMO },
  "my.updates": { what: { tr: "Kampüs gündemi", en: "The campus digest" }, source: DEVRIMO },
  "my.preferences": { what: { tr: "Tercihlerin", en: "Your preferences" }, source: DEVRIMO },
  "my.memory": { what: { tr: "Asistanın notları", en: "The assistant's notes" }, source: DEVRIMO },
  "my.update_state": { what: { tr: "Gündem durumun", en: "Your digest state" }, source: DEVRIMO },
  "campus.knowledge": { what: { tr: "Kampüs bilgi tabanı", en: "The campus knowledge base" }, source: DEVRIMO },
  "campus.page": { what: { tr: "Bir ODTÜ sayfası", en: "A METU page" }, source: { tr: "odtu.edu.tr", en: "odtu.edu.tr" } },
  researcher: { what: { tr: "Akademisyen kaydı", en: "A researcher record" }, source: AVESIS },
};

const UNKNOWN_RESOURCE: ResourceCopy = {
  what: { tr: "Bir kampüs kaydı", en: "A campus record" },
};

/** Field labels for argument rows. Written out, never CSS-cased. */
const FIELD_COPY: Record<string, Copy> = {
  to: { tr: "Kime", en: "To" },
  cc: { tr: "Bilgi (CC)", en: "Cc" },
  bcc: { tr: "Gizli (BCC)", en: "Bcc" },
  subject: { tr: "Konu", en: "Subject" },
  body: { tr: "Mesaj", en: "Message" },
  body_html: { tr: "Mesaj (HTML)", en: "Message (HTML)" },
  reply_to: { tr: "Yanıt adresi", en: "Reply to" },
  query: { tr: "Arama", en: "Search" },
  kind: { tr: "Kaynak", en: "Resource" },
  key: { tr: "Anahtar", en: "Key" },
  department: { tr: "Bölüm", en: "Department" },
  term: { tr: "Dönem", en: "Term" },
  section: { tr: "Şube", en: "Section" },
  category: { tr: "Kategori", en: "Category" },
  program_type: { tr: "Program türü", en: "Programme type" },
  folder: { tr: "Klasör", en: "Folder" },
  attachment: { tr: "Ek", en: "Attachment" },
  limit: { tr: "En fazla", en: "Limit" },
  expression: { tr: "İşlem", en: "Expression" },
  record_types: { tr: "Kayıt türleri", en: "Record types" },
  starts_after: { tr: "Şu tarihten sonra", en: "Starts after" },
  starts_before: { tr: "Şu tarihten önce", en: "Starts before" },
};

/** Arguments that describe the request's plumbing rather than its content. */
const PLUMBING_FIELDS = new Set(["expected_revision", "idempotency_key", "changes"]);

export type ToolStatusType = "running" | "complete" | "incomplete" | "requires-action";

export type ActivityDescription = {
  /** The whole line, already a sentence: "Kayıtlı ders programın okundu". */
  line: Copy;
  /** Which system answered, when we can say so honestly. */
  source?: Copy;
  /** True when nothing better than the raw tool name was available. */
  unrecognised?: boolean;
};

/** Parse a tool call's argument text without ever throwing on partial JSON. */
export function parseArgs(argsText?: string): Record<string, unknown> {
  if (!argsText) return {};
  const trimmed = argsText.trim();
  if (!trimmed.startsWith("{")) return {};
  try {
    const parsed = JSON.parse(trimmed);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : {};
  } catch {
    // Arguments stream in character by character, so mid-stream text is
    // routinely not valid JSON yet. That is not an error worth surfacing.
    return {};
  }
}

function resourceOf(args: Record<string, unknown>): ResourceCopy | undefined {
  const nested = args.resource;
  const resource = (nested && typeof nested === "object" && !Array.isArray(nested) ? nested : args) as Record<string, unknown>;
  const kind = typeof resource.kind === "string" ? resource.kind : undefined;
  if (!kind) return undefined;
  return RESOURCE_COPY[kind] ?? UNKNOWN_RESOURCE;
}

function lower(value: string) {
  return value.charAt(0).toLowerCase() + value.slice(1);
}

/**
 * One tool call as a sentence a first-year can read.
 *
 * The verb comes from the tool, the object from the resource, and the tense from
 * the status — a running call says what is happening now, because "Kayıtlı ders
 * programın okunuyor" is the reassurance a fourteen-second wait needs.
 */
export function describeActivity(
  toolName: string,
  argsText: string | undefined,
  status: ToolStatusType,
): ActivityDescription {
  const args = parseArgs(argsText);
  const resource = resourceOf(args);
  const what = resource?.what;
  const source = resource?.source;
  const running = status === "running";
  const pending = status === "requires-action";
  // "incomplete" covers both a failure and a cancellation. Both mean the answer
  // did not arrive, and the product rule says that must never read as though it
  // did — so the line says so rather than leaning on a strike-through.
  const failed = status === "incomplete";

  switch (toolName) {
    case "read":
      if (!what) break;
      return {
        line: failed
          ? { tr: `${what.tr} okunamadı`, en: `${what.en} could not be read` }
          : running
            ? { tr: `${what.tr} okunuyor…`, en: `Reading ${lower(what.en)}…` }
            : { tr: `${what.tr} okundu`, en: `${what.en} was read` },
        source,
      };
    case "search":
      if (!what) break;
      return {
        line: failed
          ? { tr: `${what.tr} aranamadı`, en: `${what.en} could not be searched` }
          : running
            ? { tr: `${what.tr} aranıyor…`, en: `Searching ${lower(what.en)}…` }
            : { tr: `${what.tr} arandı`, en: `${what.en} was searched` },
        source,
      };
    case "plan":
      return {
        line: failed
          ? { tr: "Dönem planın hesaplanamadı", en: "Your semester plan could not be worked out" }
          : running
            ? { tr: "Dönem planın hesaplanıyor…", en: "Working out your semester plan…" }
            : { tr: "Dönem planın hesaplandı", en: "Your semester plan was worked out" },
        source: { tr: "müfredatın ve katalog", en: "your curriculum and the catalog" },
      };
    case "update":
      if (failed) {
        return {
          line: { tr: `${what?.tr ?? "Planlayıcıdaki haftan"} kaydedilemedi`, en: `${what?.en ?? "The week in your planner"} could not be saved` },
          source: source ?? PLANNER,
        };
      }
      return {
        line: running
          ? { tr: `${what?.tr ?? "Planlayıcıdaki haftan"} kaydediliyor…`, en: `Saving ${lower(what?.en ?? "The week in your planner")}…` }
          : { tr: `${what?.tr ?? "Planlayıcıdaki haftan"} kaydedildi`, en: `${what?.en ?? "The week in your planner"} was saved` },
        source: source ?? PLANNER,
      };
    case "undo":
      if (failed) {
        return {
          line: { tr: "Geri alma tamamlanamadı", en: "The undo did not complete" },
          source: source ?? PLANNER,
        };
      }
      return {
        line: running
          ? { tr: "Son kayıt geri alınıyor…", en: "Undoing the last save…" }
          : { tr: "Son kayıt geri alındı", en: "The last save was undone" },
        source: source ?? PLANNER,
      };
    case "send_email":
      if (failed) {
        return {
          line: { tr: "E-posta gönderilmedi", en: "The email was not sent" },
          source: WEBMAIL,
        };
      }
      return {
        line: pending
          ? { tr: "E-posta gönderilmek üzere onayını bekliyor", en: "An email is waiting for your approval" }
          : running
            ? { tr: "E-posta gönderiliyor…", en: "Sending the email…" }
            : { tr: "E-posta gönderildi", en: "The email was sent" },
        source: WEBMAIL,
      };
    case "compute":
      return {
        line: failed
          ? { tr: "Hesap yapılamadı", en: "The arithmetic could not be done" }
          : running
            ? { tr: "Hesap yapılıyor…", en: "Doing the arithmetic…" }
            : { tr: "Hesap yapıldı", en: "The arithmetic was done" },
      };
  }

  // Nothing recognised. The raw name is shown rather than a guess, because a
  // wrong sentence about a campus read is worse than an honest technical one.
  return {
    line: failed
      ? { tr: `İşlem tamamlanamadı: ${toolName}`, en: `Did not finish: ${toolName}` }
      : running
        ? { tr: `İşlem sürüyor: ${toolName}`, en: `Working: ${toolName}` }
        : { tr: `İşlem tamamlandı: ${toolName}`, en: `Finished: ${toolName}` },
    unrecognised: true,
  };
}

export type ActivityField = { key: string; label: Copy; value: string };

function fieldValue(value: unknown): string {
  if (Array.isArray(value)) return value.map((item) => fieldValue(item)).join(", ");
  if (typeof value === "object" && value !== null) return JSON.stringify(value, null, 2);
  return String(value);
}

/**
 * Argument rows worth showing a student, in a stable order.
 *
 * Plumbing (revision numbers, idempotency keys) is dropped: it explains nothing
 * about what the assistant is doing, and it was the bulk of what a raw JSON dump
 * spent the student's attention on.
 */
export function activityFields(args: Record<string, unknown>): ActivityField[] {
  const flat: Record<string, unknown> = { ...args };
  for (const nested of ["resource", "draft", "request"]) {
    const value = flat[nested];
    if (value && typeof value === "object" && !Array.isArray(value)) {
      Object.assign(flat, value as Record<string, unknown>);
      delete flat[nested];
    }
  }

  const order = Object.keys(FIELD_COPY);
  return Object.entries(flat)
    .filter(([key, value]) => !PLUMBING_FIELDS.has(key) && value !== null && value !== undefined && value !== "")
    .filter(([, value]) => !(Array.isArray(value) && value.length === 0))
    .sort((a, b) => {
      const left = order.indexOf(a[0]);
      const right = order.indexOf(b[0]);
      return (left === -1 ? order.length : left) - (right === -1 ? order.length : right);
    })
    .map(([key, value]) => ({
      key,
      // An unmapped key keeps its own spelling with underscores opened up. No
      // CSS casing: `capitalize` under lang="tr" is what produced "İmportance".
      label: FIELD_COPY[key] ?? { tr: key.replace(/_/g, " "), en: key.replace(/_/g, " ") },
      value: fieldValue(value),
    }));
}

/**
 * The resource kind's own description without a verb, for a caller that wants
 * the source alone — a provenance footnote under an answer, for instance.
 */
export function describeResource(args: Record<string, unknown>): ResourceCopy | undefined {
  return resourceOf(args);
}
