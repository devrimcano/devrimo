"use client";

import { PlanSaveTracker } from "@/lib/planning-sync";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDownIcon, ChevronLeftIcon, ChevronRightIcon, ClipboardIcon, DownloadIcon, HeartIcon,
  Loader2Icon, PlusIcon, RotateCcwIcon, SearchIcon, Trash2Icon, TriangleAlertIcon,
} from "lucide-react";
import { toast } from "sonner";
import { useLocale } from "@/components/locale-provider";
import { Button } from "@/components/ui/button";
import {
  AlertDialog, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import { captureProductEvent, captureRequestFailure } from "@/components/posthog-analytics";
import { jsonFetch } from "@/lib/api/fetcher";
import { usePlanning, type PlanEntry, type PlanEnvelope, type PlanState } from "@/hooks/use-planning";
import { PlannerAssistant } from "@/components/schedule/planner-assistant";
import { PlannerIntro } from "@/components/schedule/planner-intro";
import { formatMetuCourseCode } from "@/lib/metu-course-code";

type Day = "Mon" | "Tue" | "Wed" | "Thu" | "Fri";
// `instructor` is optional because plans saved before it existed are still in
// students' browsers and must keep loading.
type Entry = { id: string; code: string; name: string; section: string; day: Day; start: number; duration: number; startMinute?: number; durationMinutes?: number; room: string; credits: number; color: number; kind: "course" | "block"; instructor?: string };
type CatalogCourse = { code: string; name: string; credits: number; rawCode: string };
type CatalogSection = { section: string; instructor: string; meetings: { day: Day; start: number; duration: number; startMinute?: number; durationMinutes?: number; room: string }[]; constraint: string; eligible?: boolean; reason?: string };
type ApiCatalogSection = { section?: unknown; instructor?: unknown; meetings?: unknown; constraint?: unknown; eligible?: unknown; reason?: unknown };
type SectionMap = Record<string, CatalogSection[]>;
// Keyed by course identity, then by section number.
type ConstraintRow = { given_dept?: string; start_char?: string; end_char?: string; min_cgpa?: string; max_cgpa?: string; min_year?: string; max_year?: string };
type SectionVerdict = { rows: ConstraintRow[]; eligible: boolean; reason: string };
type ConstraintMap = Record<string, Record<string, SectionVerdict>>;
type StudentDepartment = { code: string; label: string };
type AiPlanCourse = { code?: string; display_code?: string; name?: string; credits?: number; sections?: unknown };
type PrerequisiteRejection = {
  course_code?: string;
  course_label?: string;
  prerequisite_course_codes?: string[];
  prerequisite_course_labels?: string[];
};
type SubmittedMutation = { term: string; fingerprint: string; idempotencyKey: string };

const DAYS: Day[] = ["Mon", "Tue", "Wed", "Thu", "Fri"];
const DEFAULT_HOURS = Array.from({ length: 10 }, (_, index) => index + 8);
// The shortest a class hour may be drawn. Rows share the height the card
// actually has, so the week fits a desktop screen without scrolling; this is
// only the floor for when it genuinely cannot. Ten rows at this floor plus the
// day header is under 400px, which leaves the whole week visible on a laptop
// once everything around it has been kept small.
//
// It is a *track* minimum, deliberately not a min-height on the cell. That was
// the original bug: `minmax(0,1fr)` let a track shrink to 39px while
// `min-h-14` held the cell at 56px, so the cell overflowed its own row — and a
// session sized with `calc(duration * 100% + …)` measured the 56px cell while
// the rules were drawn 39px apart. Two hours then covered three and a half
// rows. With the minimum on the track, cell and row are the same box again.
const ROW_MIN_PX = 34;
// How many courses one constraints request covers. Chunked rather than sent
// whole so the red flags appear as they are decided instead of after minutes of
// nothing, and no single request is long enough to be cut off. Six rather than
// the original three because the broker now holds its SAIS session between
// reads, which halved what a cold section costs.
const CONSTRAINT_CHUNK = 6;
// Eight hues rather than five, and stronger than before: a fourteen percent
// wash read as "some tinted boxes" rather than as one colour per course,
// which is the whole job of the colour. The solid left edge is what the eye
// actually tracks down a column, and it stays legible where the fill does
// not — a narrow lane, a dark theme, a phone in sunlight.
const COLORS = [
  "bg-rose-500/20 border-rose-500/45 border-l-rose-500 text-rose-950 dark:bg-rose-500/25 dark:text-rose-50",
  "bg-sky-500/20 border-sky-500/45 border-l-sky-500 text-sky-950 dark:bg-sky-500/25 dark:text-sky-50",
  "bg-amber-500/22 border-amber-500/50 border-l-amber-500 text-amber-950 dark:bg-amber-500/25 dark:text-amber-50",
  "bg-emerald-500/20 border-emerald-500/45 border-l-emerald-500 text-emerald-950 dark:bg-emerald-500/25 dark:text-emerald-50",
  "bg-violet-500/20 border-violet-500/45 border-l-violet-500 text-violet-950 dark:bg-violet-500/25 dark:text-violet-50",
  "bg-orange-500/20 border-orange-500/45 border-l-orange-500 text-orange-950 dark:bg-orange-500/25 dark:text-orange-50",
  "bg-teal-500/20 border-teal-500/45 border-l-teal-500 text-teal-950 dark:bg-teal-500/25 dark:text-teal-50",
  "bg-fuchsia-500/20 border-fuchsia-500/45 border-l-fuchsia-500 text-fuchsia-950 dark:bg-fuchsia-500/25 dark:text-fuchsia-50",
];
// METU term codes are a four-digit year plus a part number, where the year is
// the one the academic year starts in: 20261 is 2026-2027 Fall, and 20253 is
// the summer school that runs during calendar 2026. There is exactly one term
// a student can be registering for at any point in the year, so this is
// derived and displayed rather than offered as a choice.
function upcomingTerm(now: Date = new Date()) {
  const year = now.getFullYear();
  const month = now.getMonth() + 1;
  if (month >= 8) return `${year}1`;
  if (month <= 5) return `${year - 1}2`;
  return `${year - 1}3`;
}

function termLabel(code: string, t: (tr: string, en: string) => string) {
  const year = Number(code.slice(0, 4));
  if (!Number.isFinite(year)) return code;
  if (code.endsWith("2")) return `${year}-${year + 1} ${t("Bahar", "Spring")}`;
  if (code.endsWith("3")) return `${year + 1} ${t("Yaz Okulu", "Summer")}`;
  return `${year}-${year + 1} ${t("Güz", "Fall")}`;
}

// Full METU codes are globally unique, so a course's identity is its whole
// code. Reducing it to the final three digits would confuse a service course
// with a home-department one: two unrelated departments both have a 201.
/**
 * Which lane each session occupies, so overlapping ones sit side by side.
 *
 * Lanes used to be counted per starting cell, which only sees sessions that
 * *begin* in that hour. A two-hour lecture from 08:40 and a one-hour one from
 * 09:40 therefore both believed they were alone and both drew full width, one
 * straight over the other. Overlap is about the hours a session covers, not the
 * hour it starts, so the assignment is made across the whole day: the first
 * lane whose previous session has already ended, and a new lane when there is
 * none.
 */
function laneLayout(entries: Entry[]): Map<string, { lane: number; lanes: number }> {
  const layout = new Map<string, { lane: number; lanes: number }>();
  for (const day of DAYS) {
    const ordered = entries
      .filter((entry) => entry.day === day)
      .sort((a, b) => a.start - b.start || b.duration - a.duration);
    const freeAt: number[] = [];
    const placed: { entry: Entry; lane: number }[] = [];
    for (const entry of ordered) {
      let lane = freeAt.findIndex((end) => end <= entry.start);
      if (lane === -1) lane = freeAt.length;
      freeAt[lane] = entry.start + entry.duration;
      placed.push({ entry, lane });
    }
    for (const { entry, lane } of placed) {
      // Width comes from how many lanes are busy during this session's own
      // hours, so a course alone in its slot still fills the cell even when
      // the day has three lanes elsewhere.
      const concurrent = placed.filter(({ entry: other }) =>
        other.start < entry.start + entry.duration && entry.start < other.start + other.duration);
      layout.set(entry.id, { lane, lanes: Math.max(...concurrent.map((item) => item.lane)) + 1 });
    }
  }
  return layout;
}

function courseIdentity(code: string) {
  return code.toUpperCase().replace(/[^A-Z0-9]/g, "");
}

function cleanCourseName(value: string) {
  return value
    .replace(/\(\s*\)/g, "")
    .replace(/\b[lL]{3}\b/g, "III")
    .replace(/\s+([,)])/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

function localizedCourseName(value: string, locale: "tr" | "en") {
  const cleaned = cleanCourseName(value);
  const bilingual = cleaned.match(/^(.+?)\s*\(([^()]*)\)$/);
  if (!bilingual) return cleaned;
  return (locale === "tr" ? bilingual[2] : bilingual[1]).trim() || cleaned;
}

function localizedRestrictionReason(
  value: string,
  t: (tr: string, en: string) => string,
) {
  const reason = value.trim().replace(/[.;]+$/, "");
  let match = reason.match(/^surnames\s+(.+?)\s+only$/i);
  if (match) return t(`Soyadın ${match[1]} aralığında değil`, `Your surname is outside the ${match[1]} range`);
  match = reason.match(/^open only to\s+(.+)$/i);
  if (match) return t(`Yalnızca ${match[1]} bölümlerine açık`, `Open only to ${match[1]}`);
  match = reason.match(/^CGPA\s+([\d.]+)\s+or above$/i);
  if (match) return t(`Genel not ortalaman en az ${match[1]} olmalı`, `Your CGPA must be at least ${match[1]}`);
  match = reason.match(/^CGPA\s+([\d.]+)\s+or below$/i);
  if (match) return t(`Genel not ortalaman en fazla ${match[1]} olmalı`, `Your CGPA must be at most ${match[1]}`);
  match = reason.match(/^year\s+(\d+)\s+and above$/i);
  if (match) return t(`En az ${match[1]}. sınıfta olmalısın`, `You must be in year ${match[1]} or above`);
  match = reason.match(/^year\s+(\d+)\s+and below$/i);
  if (match) return t(`En fazla ${match[1]}. sınıfta olmalısın`, `You must be in year ${match[1]} or below`);
  match = reason.match(/^for students who have not passed it; you have\s+(.+)$/i);
  if (match) return t(`Bu şube dersi geçmemiş öğrenciler için; mevcut notun ${match[1]}`, `This section is for students who have not passed the course; your grade is ${match[1]}`);
  return reason;
}

function localizedCurriculumWarning(
  value: string,
  t: (tr: string, en: string) => string,
) {
  const match = value.match(/^(history|language): kept (.+?) — .*?add (.+?) instead\.$/i);
  if (!match) return value;
  const label = (codes: string) => codes.split(", ").map((code) => {
    const course = code.trim();
    const abbreviation = ({ "240": "HIST", "629": "TURK", "642": "TURK" } as Record<string, string>)[course.slice(0, 3)];
    return abbreviation && /^\d{7}$/.test(course) ? `${abbreviation} ${course.slice(3).replace(/^0+/, "")}` : course;
  }).join(", ");
  const kept = label(match[2]);
  const alternative = label(match[3]);
  const family = match[1].toLowerCase() === "history" ? t("Tarih", "history") : t("Türkçe", "Turkish language");
  return t(
    `${family} dersi olarak ${kept} eklendi. Uluslararası öğrenciysen ${alternative} dersini elle ekleyebilirsin.`,
    `${kept} was added for the ${family} requirement. International students can add ${alternative} manually instead.`,
  );
}

// The first three digits of a seven-digit code name the department that owns
// the course. Anything shorter does not say, and the backend resolves it
// against the catalog rather than assuming the student's own department.
function owningDepartment(courseCode: string, fallback: string) {
  const digits = courseCode.replace(/\D/g, "");
  return digits.length === 7 ? digits.slice(0, 3) : fallback;
}

function itemStartMinute(item: { start: number; startMinute?: number }) {
  return item.startMinute ?? item.start * 60 + 40;
}

function itemDurationMinutes(item: { duration: number; durationMinutes?: number }) {
  return item.durationMinutes ?? Math.max(1, item.duration * 60 - 10);
}

function formatClock(totalMinutes: number) {
  const minutes = Number.isFinite(totalMinutes) ? Math.max(0, Math.trunc(totalMinutes)) : 0;
  return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}

function formatItemTime(item: { start: number; startMinute?: number }) {
  return formatClock(itemStartMinute(item));
}

function formatItemRange(item: { start: number; duration: number; startMinute?: number; durationMinutes?: number }) {
  const start = itemStartMinute(item);
  return `${formatClock(start)}–${formatClock(start + itemDurationMinutes(item))}`;
}

/** Convert the server's typed section contract to the view model. */
function fromTypedSections(value: unknown): CatalogSection[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const row = item as ApiCatalogSection;
    const section = String(row.section ?? "").trim();
    if (!section) return [];
    const meetings = Array.isArray(row.meetings) ? row.meetings.flatMap((rawMeeting) => {
      if (!rawMeeting || typeof rawMeeting !== "object") return [];
      const meeting = rawMeeting as Record<string, unknown>;
      const day = meeting.day;
      if (!DAYS.includes(day as Day)) return [];
      const startMinute = Number(meeting.start_minute);
      const durationMinutes = Number(meeting.duration_minutes);
      if (!Number.isFinite(startMinute) || !Number.isFinite(durationMinutes) || durationMinutes < 1) return [];
      return [{
        day: day as Day,
        start: Math.floor(startMinute / 60),
        duration: Math.max(1, Math.ceil(durationMinutes / 60)),
        startMinute,
        durationMinutes,
        room: String(meeting.room ?? ""),
      }];
    }) : [];
    return [{
      section,
      instructor: String(row.instructor ?? ""),
      meetings,
      constraint: String(row.constraint ?? ""),
      eligible: typeof row.eligible === "boolean" ? row.eligible : undefined,
      reason: String(row.reason ?? ""),
    }];
  });
}

function overlaps(a: { day: Day; start: number; duration: number; startMinute?: number; durationMinutes?: number }, b: { day: Day; start: number; duration: number; startMinute?: number; durationMinutes?: number }) {
  const aStart = itemStartMinute(a);
  const bStart = itemStartMinute(b);
  return a.day === b.day && aStart < bStart + itemDurationMinutes(b) && bStart < aStart + itemDurationMinutes(a);
}

function toCanonicalEntry(entry: Entry): PlanEntry {
  return {
    id: entry.id,
    code: entry.code,
    name: entry.name,
    section: entry.section,
    credits: entry.credits,
    color: entry.color,
    kind: entry.kind,
    instructor: entry.instructor ?? "",
    day: entry.day,
    start_minute: itemStartMinute(entry),
    duration_minutes: itemDurationMinutes(entry),
    room: entry.room ?? "",
  };
}

function fromCanonicalEntry(entry: PlanEntry): Entry {
  return {
    ...entry,
    start: Math.floor(entry.start_minute / 60),
    duration: Math.max(1, Math.ceil(entry.duration_minutes / 60)),
    startMinute: entry.start_minute,
    durationMinutes: entry.duration_minutes,
  };
}

function canonicalStateFromLocal(values: {
  entries: Entry[];
  department: string;
  departmentLabel: string;
  emptyDays: Day[];
  avoidConflicts: boolean;
  ignoreConstraints: boolean;
  pool: CatalogCourse[];
  sections: SectionMap;
  alternatives: Entry[][];
  alternativeIndex: number;
  favorites: Entry[][];
  favoriteIndex: number;
}): PlanState {
  return {
    entries: values.entries.map(toCanonicalEntry),
    department: values.department,
    department_label: values.departmentLabel,
    empty_days: values.emptyDays,
    avoid_conflicts: values.avoidConflicts,
    ignore_constraints: values.ignoreConstraints,
    pool: values.pool.map((course) => ({ ...course, raw_code: course.rawCode })),
    sections: values.sections,
    alternatives: values.alternatives.map((alternative) => alternative.map(toCanonicalEntry)),
    alternative_index: values.alternativeIndex,
    favorites: values.favorites.map((favorite) => favorite.map(toCanonicalEntry)),
    favorite_index: values.favoriteIndex,
  };
}

/**
 * Fingerprint only canonical values.  The view keeps hour-shaped aliases for
 * rendering and the API keeps a few legacy aliases for old clients; neither
 * should make an acknowledged state look different from the submitted one.
 */
function canonicalStateFingerprint(state: PlanState): string {
  const meeting = (value: Record<string, unknown>) => ({
    day: value.day,
    start_minute: Number(value.start_minute ?? value.startMinute ?? (Number(value.start ?? 0) * 60 + 40)),
    duration_minutes: Number(value.duration_minutes ?? value.durationMinutes ?? Math.max(1, Number(value.duration ?? 1) * 60 - 10)),
    room: String(value.room ?? ""),
  });
  const entry = (value: PlanEntry) => ({
    id: value.id,
    code: value.code,
    name: value.name,
    section: value.section,
    credits: value.credits,
    color: value.color,
    kind: value.kind,
    instructor: value.instructor,
    day: value.day,
    start_minute: value.start_minute,
    duration_minutes: value.duration_minutes,
    room: value.room,
  });
  const section = (value: Record<string, unknown>) => ({
    section: String(value.section ?? value.section_number ?? ""),
    instructor: String(value.instructor ?? ""),
    meetings: (Array.isArray(value.meetings) ? value.meetings : []).flatMap((raw) => (
      raw && typeof raw === "object" ? [meeting(raw as Record<string, unknown>)] : []
    )),
    constraint: String(value.constraint ?? ""),
    eligible: value.eligible === true ? true : value.eligible === false ? false : null,
    reason: String(value.reason ?? ""),
  });
  return JSON.stringify({
    entries: state.entries.map(entry),
    department: state.department,
    department_label: state.department_label,
    empty_days: state.empty_days,
    avoid_conflicts: state.avoid_conflicts,
    ignore_constraints: state.ignore_constraints,
    pool: state.pool.map((course) => ({
      code: course.code,
      name: course.name,
      credits: course.credits,
      raw_code: course.raw_code ?? course.rawCode ?? course.code,
    })),
    sections: Object.fromEntries(Object.entries(state.sections).map(([key, rows]) => [
      key,
      rows.flatMap((raw) => raw && typeof raw === "object" ? [section(raw as Record<string, unknown>)] : []),
    ])),
    alternatives: state.alternatives.map((alternative) => alternative.map(entry)),
    alternative_index: state.alternative_index,
    favorites: state.favorites.map((favorite) => favorite.map(entry)),
    favorite_index: state.favorite_index,
  });
}

function fromCanonicalSections(value: Record<string, unknown[]>): SectionMap {
  const result: SectionMap = {};
  for (const [key, rows] of Object.entries(value)) {
    result[key] = rows.flatMap((item) => {
      if (!item || typeof item !== "object") return [];
      const row = item as Record<string, unknown>;
      const meetings = Array.isArray(row.meetings) ? row.meetings.flatMap((meeting) => {
        if (!meeting || typeof meeting !== "object") return [];
        const value = meeting as Record<string, unknown>;
        const startMinute = Number(value.start_minute ?? value.startMinute ?? (Number(value.start ?? 0) * 60 + 40));
        const durationMinutes = Number(value.duration_minutes ?? value.durationMinutes ?? Math.max(1, Number(value.duration ?? 1) * 60 - 10));
        if (!Number.isFinite(startMinute) || !Number.isFinite(durationMinutes)) return [];
        return [{
          day: value.day as Day,
          start: Math.floor(startMinute / 60),
          duration: Math.max(1, Math.ceil(durationMinutes / 60)),
          startMinute,
          durationMinutes,
          room: String(value.room ?? row.room ?? ""),
        }];
      }) : [];
      return [{
        section: String(row.section ?? row.section_number ?? ""),
        instructor: String(row.instructor ?? ""),
        meetings,
        constraint: String(row.constraint ?? ""),
        eligible: typeof row.eligible === "boolean" ? row.eligible : undefined,
        reason: String(row.reason ?? ""),
      }];
    });
  }
  return result;
}

function canonicalSectionsFromLocal(sections: SectionMap, constraints: ConstraintMap): Record<string, unknown[]> {
  return Object.fromEntries(Object.entries(sections).map(([key, rows]) => [
    key,
    rows.map((row) => ({
      ...row,
      eligible: constraints[key]?.[row.section]?.eligible ?? row.eligible,
      reason: constraints[key]?.[row.section]?.reason ?? row.reason ?? "",
      meetings: row.meetings.map((meeting) => ({
        ...meeting,
        start_minute: itemStartMinute(meeting),
        duration_minutes: itemDurationMinutes(meeting),
      })),
    })),
  ]));
}

/**
 * How pleasant a schedule is to actually attend.
 *
 * Ranking matters more than generating here: twenty-four arbitrary
 * combinations are a chore to click through, but the same twenty-four with the
 * free-day-heavy, gap-free ones first are a genuine choice. Free days come
 * before gaps because that is the trade students actually ask for.
 */
function scheduleShape(entries: Entry[]) {
  const used = new Set(entries.map((entry) => entry.day));
  let gaps = 0;
  for (const day of used) {
    const onDay = entries.filter((entry) => entry.day === day);
    const first = Math.min(...onDay.map((entry) => entry.start));
    const last = Math.max(...onDay.map((entry) => entry.start + entry.duration));
    const taught = onDay.reduce((sum, entry) => sum + entry.duration, 0);
    gaps += last - first - taught;
  }
  return { dayCount: used.size, gaps, freeDays: DAYS.filter((day) => !used.has(day)) };
}

// Turkish Windows sets Excel's list separator to ';', and a comma-delimited
// file opens there as a single column of text. The leading BOM is what makes
// Excel read the bytes as UTF-8 rather than as the local code page, which is
// the difference between "Çarşamba" and mojibake.
const csvCell = (value: unknown) => {
  const text = String(value ?? "");
  return /[";\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
};

function downloadFile(name: string, blob: Blob) {
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = name;
  link.click();
  URL.revokeObjectURL(link.href);
}

export function SchedulePlanner() {
  const { locale, pick } = useLocale();
  const t = useCallback((tr: string, en: string) => pick({ tr, en }), [pick]);

  const [term] = useState(() => upcomingTerm());
  const planning = usePlanning(term);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [favorites, setFavorites] = useState<Entry[][]>([]);
  const [favoriteIndex, setFavoriteIndex] = useState(-1);
  const [studentDepartment, setStudentDepartment] = useState<StudentDepartment | null>(null);
  const [departmentBusy, setDepartmentBusy] = useState(true);
  const department = studentDepartment?.code ?? "";
  const [emptyDays, setEmptyDays] = useState<Day[]>([]);
  const [avoidConflicts, setAvoidConflicts] = useState(true);
  const [ignoreConstraints, setIgnoreConstraints] = useState(false);
  const [catalogCourses, setCatalogCourses] = useState<CatalogCourse[]>([]);
  const [sectionsByCourse, setSectionsByCourse] = useState<SectionMap>({});
  const [expandedCourse, setExpandedCourse] = useState<string | null>(null);
  const [sectionsBusy, setSectionsBusy] = useState<string | null>(null);
  const [catalogSearch, setCatalogSearch] = useState("");
  const [planBusy, setPlanBusy] = useState(false);
  const [generateProgress, setGenerateProgress] = useState<{ done: number; total: number } | null>(null);
  const [constraints, setConstraints] = useState<ConstraintMap>({});
  // Verdicts arrive after the course list does. Saying so beats a section that
  // silently looks open until a flag appears on it a minute later.
  const [constraintsBusy, setConstraintsBusy] = useState(false);
  const [alternatives, setAlternatives] = useState<Entry[][]>([]);
  const [alternativeIndex, setAlternativeIndex] = useState(0);
  const [curriculumNotice, setCurriculumNotice] = useState("");
  const [prerequisiteRejections, setPrerequisiteRejections] = useState<PrerequisiteRejection[]>([]);
  const [mobileDay, setMobileDay] = useState<Day>("Mon");
  const [poolQuery, setPoolQuery] = useState("");
  const [suggestions, setSuggestions] = useState<CatalogCourse[]>([]);
  const [suggestBusy, setSuggestBusy] = useState(false);
  const [suggestNote, setSuggestNote] = useState("");
  const [manualOpen, setManualOpen] = useState(false);
  const [manualBusy, setManualBusy] = useState(false);
  const [draft, setDraft] = useState({ code: "", name: "", section: "1", day: "Mon" as Day, start: 9, duration: 1, room: "", credits: 3 });
  // Nothing may be sent back until the canonical server state has been read,
  // or the empty first render would overwrite the plan it is about to load.
  const [hydrated, setHydrated] = useState(false);
  const serverStateFingerprint = useRef("");
  const saveTracker = useRef(new PlanSaveTracker());
  const localFingerprintRef = useRef("");
  const initialConstraintsFetched = useRef(false);
  const poolRef = useRef<CatalogCourse[]>([]);
  useEffect(() => { poolRef.current = catalogCourses; }, [catalogCourses]);
  useEffect(() => {
    saveTracker.current.reset(term);
    serverStateFingerprint.current = "";
  }, [term]);
  const busy = planBusy || generateProgress !== null;

  // --- persistence --------------------------------------------------------
  const applyCanonicalState = useCallback((state: PlanState) => {
    const nextEntries = state.entries.map((entry) => ({ ...fromCanonicalEntry(entry), code: formatMetuCourseCode(entry.code) }));
    const nextPool = state.pool.map((course) => ({
      code: formatMetuCourseCode(course.code),
      name: course.name,
      credits: course.credits,
      rawCode: course.raw_code ?? course.rawCode ?? course.code,
    }));
    const nextSections = fromCanonicalSections(state.sections);
    const nextAlternatives = state.alternatives.map((alternative) => alternative.map((entry) => ({ ...fromCanonicalEntry(entry), code: formatMetuCourseCode(entry.code) })));
    const nextFavorites = state.favorites.map((favorite) => favorite.map((entry) => ({ ...fromCanonicalEntry(entry), code: formatMetuCourseCode(entry.code) })));
    setEntries(nextEntries);
    setCatalogCourses(nextPool);
    setSectionsByCourse(nextSections);
    setEmptyDays(state.empty_days.filter((day): day is Day => DAYS.includes(day)));
    setAvoidConflicts(state.avoid_conflicts);
    setIgnoreConstraints(state.ignore_constraints);
    setAlternatives(nextAlternatives);
    setAlternativeIndex(state.alternative_index);
    setFavorites(nextFavorites);
    setFavoriteIndex(state.favorite_index);
    serverStateFingerprint.current = canonicalStateFingerprint(state);
  }, []);

  // Server responses are applied by revision. Local edits keep their current
  // controls while a save is in flight; a successful response (or undo) then
  // becomes the new rendered source of truth.
  const planningReady = planning.ready;
  const planningRevision = planning.revision;
  const planningTerm = planning.envelope?.term;
  const planningIdentity = `${planningTerm ?? ""}:${planningRevision}`;
  const planningUpdate = planning.update;

  const canonicalSections = useMemo(
    () => canonicalSectionsFromLocal(sectionsByCourse, constraints),
    [constraints, sectionsByCourse],
  );
  const localCanonicalState = useMemo(() => canonicalStateFromLocal({
    entries,
    department,
    departmentLabel: studentDepartment?.label ?? department,
    emptyDays,
    avoidConflicts,
    ignoreConstraints,
    pool: catalogCourses,
    sections: canonicalSections as SectionMap,
    alternatives,
    alternativeIndex,
    favorites,
    favoriteIndex,
  }), [
    alternatives,
    alternativeIndex,
    avoidConflicts,
    canonicalSections,
    catalogCourses,
    department,
    emptyDays,
    entries,
    favoriteIndex,
    favorites,
    ignoreConstraints,
    studentDepartment?.label,
  ]);

  const localFingerprint = useMemo(() => canonicalStateFingerprint(localCanonicalState), [localCanonicalState]);
  useEffect(() => {
    localFingerprintRef.current = localFingerprint;
  }, [localFingerprint]);

  const reconcileResponse = useCallback((next: PlanEnvelope, fingerprint: string) => {
    const decision = saveTracker.current.acknowledge(next, fingerprint);
    if (decision === "ignore") return;
    if (decision === "adopt") applyCanonicalState(next.state);
    else serverStateFingerprint.current = canonicalStateFingerprint(next.state);
    setHydrated(true);
  }, [applyCanonicalState]);

  useEffect(() => {
    if (planningReady && planning.envelope && planningTerm === term) {
      reconcileResponse(planning.envelope, localFingerprint);
    }
  }, [localFingerprint, planning.envelope, planningReady, planningTerm, reconcileResponse, term]);

  const reconcileSubmittedMutation = useCallback((_submitted: SubmittedMutation, next: PlanEnvelope) => {
    reconcileResponse(next, localFingerprintRef.current);
  }, [reconcileResponse]);

  /** Submit an explicit planner mutation and reconcile it with local edits. */
  const submitPlanningUpdate = useCallback((changes: Parameters<typeof planningUpdate>[0]) => {
    if (planning.saving || planning.retryable || planning.conflict || planning.saveError) return Promise.resolve(null);
    const submitted: SubmittedMutation = { term, fingerprint: localFingerprintRef.current, idempotencyKey: crypto.randomUUID() };
    saveTracker.current.submit(submitted.idempotencyKey, submitted.term, submitted.fingerprint);
    const result = planningUpdate(changes, submitted.idempotencyKey);
    void result.then((next) => {
      if (!next) return;
      reconcileSubmittedMutation(submitted, next);
    });
    return result;
  }, [planning.conflict, planning.retryable, planning.saveError, planning.saving, planningUpdate, reconcileSubmittedMutation, term]);

  // Every planner edit is persisted as one canonical state update. This is a
  // short debounce for rapid UI changes, while the server still orders each
  // request with its revision and idempotency key.
  useEffect(() => {
    if (!hydrated || !planningReady || planning.saving || planning.retryable || planning.conflict) return;
    const fingerprint = localFingerprint;
    if (fingerprint === serverStateFingerprint.current) return;
    const timer = window.setTimeout(() => {
      const submitted: SubmittedMutation = { term, fingerprint, idempotencyKey: crypto.randomUUID() };
      saveTracker.current.submit(submitted.idempotencyKey, submitted.term, submitted.fingerprint);
      void planningUpdate({ operation: "replace", state: localCanonicalState }, submitted.idempotencyKey);
    }, 350);
    return () => window.clearTimeout(timer);
  }, [hydrated, localCanonicalState, localFingerprint, planning.conflict, planning.retryable, planning.saving, planningIdentity, planningReady, planningUpdate, term]);

  useEffect(() => {
    let cancelled = false;
    void jsonFetch<{ department_query: string | null; department_code: string | null; surname_prefix: string | null }>("/api/schedule/student-context")
      .then((context) => {
        if (cancelled) return;
        // Resolved against the catalog server-side. Scanning the payload here
        // for any three-digit run used to pick up a year or a row count and
        // then quietly load another department's courses.
        // Some SAIS records expose an official abbreviation (for example EE)
        // while the catalog lookup cannot resolve a numeric department code.
        // The AI planner accepts that verified SAIS value, so do not leave the
        // actionable state empty while displaying a valid department label.
        const resolved = context.department_code ?? context.department_query ?? "";
        if (resolved) {
          setStudentDepartment({ code: resolved, label: context.department_query ?? resolved });
        } else {
          setStudentDepartment(null);
        }
      })
      .catch((error) => {
        captureRequestFailure(error, { operation: "schedule.student_context", kind: "query" });
        if (!cancelled) setStudentDepartment(null);
      })
      .finally(() => { if (!cancelled) setDepartmentBusy(false); });
    // The student context is stable for the lifetime of this page.
    return () => { cancelled = true; };
  }, []);

  // --- derived ------------------------------------------------------------

  const conflicts = useMemo(() => new Set(entries.flatMap((entry, index) => entries.slice(index + 1).filter((other) => overlaps(entry, other)).flatMap((other) => [entry.id, other.id]))), [entries]);
  const uniqueCourses = new Set(entries.filter((entry) => entry.kind === "course").map((entry) => entry.code)).size;
  const totalCredits = entries.filter((entry) => entry.kind === "course").reduce((sum, entry) => sum + entry.credits, 0);
  const totalHours = entries.reduce((sum, entry) => sum + entry.duration, 0);
  const scheduledCourseGroups = useMemo(() => {
    const groups = new Map<string, Entry[]>();
    for (const entry of entries) {
      const key = `${entry.code}::${entry.section}`;
      groups.set(key, [...(groups.get(key) ?? []), entry]);
    }
    return [...groups.values()];
  }, [entries]);
  const scheduledCodes = new Set(entries.filter((entry) => entry.kind === "course").map((entry) => courseIdentity(entry.code)));
  const selectedPoolCount = catalogCourses.filter((course) =>
    scheduledCodes.has(courseIdentity(course.code)) || scheduledCodes.has(courseIdentity(course.rawCode))).length;
  const mobileEntries = entries
    .filter((entry) => entry.day === mobileDay)
    .sort((a, b) => itemStartMinute(a) - itemStartMinute(b) || a.code.localeCompare(b.code));

  const dayLabel = useCallback((day: Day) => ({ Mon: t("Pzt", "Mon"), Tue: t("Sal", "Tue"), Wed: t("Çar", "Wed"), Thu: t("Per", "Thu"), Fri: t("Cum", "Fri") })[day], [t]);
  // What distinguishes one alternative from the next, in the terms a student
  // is choosing on: which days it leaves free.
  const currentShape = useMemo(() => (entries.length ? scheduleShape(entries) : null), [entries]);
  // Recomputed only when the timetable changes, not per cell: the grid has
  // fifty cells and this is a whole-week assignment.
  const gridLanes = useMemo(() => laneLayout(entries), [entries]);
  // Extend the default 08–17 range when entries fall outside it, so every
  // session on the timetable is actually drawn.
  const hours = useMemo(() => {
    let lo = DEFAULT_HOURS[0];
    let hi = DEFAULT_HOURS[DEFAULT_HOURS.length - 1];
    for (const entry of entries) {
      if (entry.start < lo) lo = entry.start;
      const end = entry.start + entry.duration - 1;
      if (end > hi) hi = end;
    }
    return Array.from({ length: hi - lo + 1 }, (_, i) => lo + i);
  }, [entries]);
  const visibleCourses = catalogCourses.filter((course) => `${course.code} ${course.name}`.toLowerCase().includes(catalogSearch.toLowerCase()));

  // --- course pool --------------------------------------------------------

  /**
   * Add one row typed by hand.
   *
   * Checked against METU's eligibility table exactly like a section picked
   * from the catalog. This used to skip the check entirely, so the one route
   * into the timetable that nobody was watching was also the easiest one: a
   * section closed to the student went straight onto the grid with no flag and
   * no refusal, and the schedule they built their week around was wrong.
   */
  async function addEntry() {
    const code = draft.code.trim().toUpperCase().replace(/\s+/g, " ");
    if (!code) return toast.error(t("Ders kodu veya blok adı gerekli.", "A course code or block name is required."));
    const next: Entry = { ...draft, id: crypto.randomUUID(), code, name: draft.name.trim() || code, room: draft.room.trim(), color: uniqueCourses % COLORS.length, kind: code.startsWith("BLOCK:") ? "block" : "course" };
    if (emptyDays.includes(next.day)) return toast.error(t("Bu günü boş gün olarak seçtin.", "You selected this as an empty day."));
    if (avoidConflicts && entries.some((entry) => overlaps(entry, next))) return toast.error(t("Bu saat mevcut bir dersle çakışıyor.", "This time conflicts with an existing course."));

    const section = next.section.trim();
    if (next.kind === "course" && section) {
      setManualBusy(true);
      try {
        const verdicts = await loadConstraints(code);
        const verdict = verdicts[section];
        if (verdict && !verdict.eligible && !ignoreConstraints) {
          const reason = localizedRestrictionReason(verdict.reason, t);
          return toast.error(verdict.reason
            ? t(`Şube ${section} sana kapalı: ${reason}. Yine de eklemek için "Şube kısıtlarını yok say"ı aç.`,
                `Section ${section} is closed to you: ${reason}. Turn on "Ignore section restrictions" to add it anyway.`)
            : t(`Şube ${section} kısıtlarına uymuyorsun.`, `You do not meet section ${section}'s restrictions.`));
        }
        if (verdict && !verdict.eligible) {
          toast.warning(t(`Şube ${section} kısıtlara uymuyor, kısıtlar yok sayıldığı için eklendi.`,
            `Section ${section} does not meet its restrictions; added because restrictions are ignored.`));
        }
      } finally {
        setManualBusy(false);
      }
    }

    setEntries((current) => [...current, next]);
    setDraft((current) => ({ ...current, code: "", name: "", room: "" }));
  }

  /**
   * Put a course in the pool.
   *
   * Prefers a suggestion the student picked, which carries the real title and
   * credit count. Typing a code and pressing Enter still works and takes the
   * first suggestion when there is one — the box used to store whatever was
   * typed verbatim, so a mistyped code became a course with no name, no
   * credits and no sections, and nothing said why.
   */
  function addPoolCourse(picked?: CatalogCourse) {
    const chosen = picked ?? suggestions[0];
    // Without a catalog-verified suggestion the typed text becomes a pool
    // entry with no name, no credits and no sections — which looks like a
    // bug rather than a feature. Refuse it rather than silently creating a
    // broken entry the student has to notice and delete.
    if (!chosen) {
      if (!poolQuery.trim()) return toast.error(t("Ders kodu gerekli.", "Course code is required."));
      return toast.error(t("Önce listeden bir ders seç veya aramayı bekle.", "Pick a course from the list or wait for search results."));
    }
    const rawCode = chosen.rawCode.toUpperCase().replace(/[^A-Z0-9]/g, "");
    if (!rawCode) return toast.error(t("Ders kodu gerekli.", "Course code is required."));
    if (catalogCourses.some((course) => courseIdentity(course.rawCode) === courseIdentity(rawCode))) return toast.error(t("Bu ders zaten listede.", "This course is already in the list."));
    setCatalogCourses((current) => [...current, chosen]);
    setPoolQuery("");
    setSuggestions([]);
    void fetchAllConstraints([chosen]);
  }

  // Suggestions as the student types. Debounced because every keystroke would
  // otherwise be a request, and the catalog answers per department: "HIST"
  // lists History, "PHYS21" narrows to PHYS210 and PHYS213, and a bare number
  // searches the student's own department.
  useEffect(() => {
    const typed = poolQuery.trim();
    let cancelled = false;
    // Everything happens inside the timer, including clearing stale results:
    // this file's lint forbids a synchronous setState in an effect body, and
    // deferring it also stops a half-typed code from firing a request.
    const timer = window.setTimeout(async () => {
      if (typed.length < 2) { setSuggestions([]); setSuggestNote(""); return; }
      setSuggestBusy(true);
      try {
        const response = await jsonFetch<{ department: string; courses: { code: string; full_code: string; name: string; credits: number }[] }>(
          `/api/schedule/courses/search?query=${encodeURIComponent(typed)}&semester=${encodeURIComponent(term)}`,
        );
        if (cancelled) return;
        setSuggestions(response.courses.map((item) => ({ rawCode: item.full_code, code: item.code, name: item.name, credits: item.credits })));
        setSuggestNote(response.courses.length ? "" : t(`${response.department} altında eşleşen ders yok.`, `No matching course under ${response.department}.`));
      } catch (error) {
        if (cancelled) return;
        setSuggestions([]);
        setSuggestNote(error instanceof Error ? error.message : t("Öneriler alınamadı.", "Suggestions unavailable."));
      } finally {
        if (!cancelled) setSuggestBusy(false);
      }
    }, 300);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [poolQuery, term, t]);

  function removePoolCourse(course: CatalogCourse) {
    const identity = courseIdentity(course.rawCode);
    setCatalogCourses((current) => current.filter((item) => courseIdentity(item.rawCode) !== identity));
    setSectionsByCourse((current) => { const next = { ...current }; delete next[identity]; return next; });
    if (expandedCourse === identity) setExpandedCourse(null);
  }

  function clearCoursePool() {
    setCatalogCourses([]);
    setSectionsByCourse({});
    setConstraints({});
    setExpandedCourse(null);
    setCatalogSearch("");
    setCurriculumNotice("");
    // The alternatives were arrangements of the pool that just went away.
    setAlternatives([]);
    setAlternativeIndex(0);
    toast.success(t("Ders havuzu temizlendi.", "Course pool cleared."));
  }

  function removeScheduledCourse(courseEntries: Entry[]) {
    const ids = new Set(courseEntries.map((entry) => entry.id));
    setEntries((current) => current.filter((entry) => !ids.has(entry.id)));
  }

  // --- campus data --------------------------------------------------------

  /** This course's sections, fetched from the catalog when we hold none yet. */
  const fetchSections = useCallback(async (course: CatalogCourse, known: SectionMap): Promise<CatalogSection[]> => {
    const identity = courseIdentity(course.rawCode);
    const cached = known[identity];
    if (cached?.length) return cached;
    const courseDepartment = owningDepartment(course.rawCode, department);
    const response = await jsonFetch<{ sections?: unknown }>(`/api/schedule/courses/${encodeURIComponent(course.rawCode)}?department=${encodeURIComponent(courseDepartment)}&semester=${encodeURIComponent(term)}`);
    const sections = fromTypedSections(response.sections);
    setSectionsByCourse((current) => ({ ...current, [identity]: sections }));
    return sections;
  }, [department, term]);

  /**
   * Sections for a whole pool in one request.
   *
   * Generating a schedule needs every course's sections, and asking for them
   * one at a time meant fifteen round trips for a fifteen-course pool, each
   * able to open its own catalog connection. The broker reads them over one
   * connection and answers whatever it could, so a course it cannot read costs
   * that course and not the batch.
   */
  const fetchPoolSections = useCallback(async (courses: CatalogCourse[], known: SectionMap): Promise<SectionMap> => {
    const wanted = courses.filter((course) => !known[courseIdentity(course.rawCode)]?.length);
    if (!wanted.length) return known;
    const response = await jsonFetch<{ courses?: Record<string, { sections?: unknown; error?: string }> }>(
      "/api/schedule/sections",
      { method: "POST", body: { semester: term, department: department.trim() || undefined, courses: wanted.map((course) => course.rawCode) } },
    );
    const found: SectionMap = {};
    for (const [rawCode, payload] of Object.entries(response.courses ?? {})) {
      if (payload?.error !== undefined) continue;
      found[courseIdentity(rawCode)] = fromTypedSections(payload?.sections);
    }
    setSectionsByCourse((current) => ({ ...current, ...found }));
    return { ...known, ...found };
  }, [department, term]);

  /**
   * Verdicts for a whole pool of courses in one request.
   *
   * Restrictions differ from section to section, which is the normal case
   * rather than the exception, so a student cannot be told whether a course is
   * open to them until every section's table has been read. Fetching that per
   * course from here meant one request each; the broker does them together
   * over a single catalog connection instead.
   */
  const fetchAllConstraints = useCallback(async (courses: CatalogCourse[]) => {
    const wanted = courses.map((course) => course.rawCode).filter(Boolean);
    if (!wanted.length) return;
    setConstraintsBusy(true);
    // In small groups rather than one request. A cold read is one SAIS page
    // per section and a course can have forty-five, so a whole curriculum in
    // one call would run for minutes and show nothing until it finished.
    // Chunked, the red flags appear as they are decided, and no single
    // request is long enough to be cut off.
    try {
      for (let index = 0; index < wanted.length; index += CONSTRAINT_CHUNK) {
        const chunk = wanted.slice(index, index + CONSTRAINT_CHUNK);
        try {
          const response = await jsonFetch<{ courses?: Record<string, { sections?: Record<string, SectionVerdict> }> }>(
            "/api/schedule/constraints",
            { method: "POST", body: { semester: term, department: department.trim() || undefined, courses: chunk } },
          );
          setConstraints((current) => {
            const updated = { ...current };
            for (const [rawCode, payload] of Object.entries(response.courses ?? {})) {
              const identity = courseIdentity(rawCode);
              // Ignore if the course was removed from the pool during the request
              if (!poolRef.current.some((c) => courseIdentity(c.rawCode) === identity)) continue;
              const sections = payload?.sections ?? {};
              // Never replace verdicts we already hold with an empty answer
              if (!Object.keys(sections).length && current[identity] && Object.keys(current[identity]).length) continue;
              updated[identity] = sections;
            }
            return updated;
          });
        } catch {
          // One failed group must not cost the rest their verdicts. A course
          // with none is treated as unrestricted, exactly as before.
        }
      }
    } finally {
      setConstraintsBusy(false);
    }
  }, [department, term]);

  /**
   * The eligibility verdicts for one course, fetched once and remembered.
   *
   * Returns them as well as storing them, because a manual add has to decide
   * *now* whether to accept the entry and cannot wait for a state update.
   */
  const loadConstraints = useCallback(async (rawCode: string): Promise<Record<string, SectionVerdict>> => {
    const identity = courseIdentity(rawCode);
    const courseDepartment = owningDepartment(rawCode, department);
    try {
      const response = await jsonFetch<{ sections?: Record<string, SectionVerdict> }>(
        `/api/schedule/courses/${encodeURIComponent(rawCode)}/constraints?department=${encodeURIComponent(courseDepartment)}&semester=${encodeURIComponent(term)}`,
      );
      const sections = response.sections ?? {};
      // Never replace verdicts we already hold with an empty answer: the bulk
      // fetch and this one race, and an empty overwrite silently turned every
      // red flag back to "unrestricted".
      setConstraints((current) => (
        !Object.keys(sections).length && current[identity] && Object.keys(current[identity]).length
          ? current
          : { ...current, [identity]: sections }
      ));
      return sections;
    } catch {
      // A course whose table cannot be read keeps its sections and simply gets
      // no verdict, which the eligibility check treats as "not restricted".
      setConstraints((current) => (current[identity] ? current : { ...current, [identity]: {} }));
      return {};
    }
  }, [department, term]);

  const fetchConstraints = useCallback(
    (course: CatalogCourse) => loadConstraints(course.rawCode),
    [loadConstraints],
  );

  useEffect(() => {
    if (hydrated && !initialConstraintsFetched.current && catalogCourses.length) {
      initialConstraintsFetched.current = true;
      void fetchAllConstraints(catalogCourses);
    }
  }, [hydrated, catalogCourses, fetchAllConstraints]);

  /** Display the server's eligibility verdict; the browser never recomputes it. */
  const sectionAllowed = useCallback((course: CatalogCourse, section: CatalogSection) => {
    const verdict = constraints[courseIdentity(course.rawCode)]?.[section.section];
    if (verdict) return { allowed: verdict.eligible, reason: verdict.reason };
    return { allowed: section.eligible !== false, reason: section.reason ?? "" };
  }, [constraints]);

  async function requestCurriculum(courses: CatalogCourse[]) {
    // This was the slowest thing a student waited on in the whole app — a
    // median of 95.8 seconds — because it ran an agent that called one catalog
    // tool per department with a model turn between each. It now reads the
    // curriculum directly and answers in about two.
    const startedAt = Date.now();
    let response: { courses?: AiPlanCourse[]; warnings?: string[]; prerequisite_rejections?: PrerequisiteRejection[]; cache_hit?: boolean; duration_ms?: number };
    try {
      response = await jsonFetch<{ courses?: AiPlanCourse[]; warnings?: string[]; prerequisite_rejections?: PrerequisiteRejection[]; cache_hit?: boolean; duration_ms?: number }>("/api/schedule/curriculum", {
        method: "POST",
        body: { semester: term, courses: courses.map((course) => ({ code: course.rawCode })) },
      });
    } catch (error) {
      captureProductEvent("schedule_plan_completed", {
        result: "error",
        requested_courses: courses.length,
        returned_courses: 0,
        warnings: 0,
        duration_seconds: (Date.now() - startedAt) / 1000,
      });
      throw error;
    }
    const sectionMap: SectionMap = {};
    const verified = (response.courses ?? []).flatMap((item) => {
      const rawCode = String(item.code ?? "").trim();
      if (!rawCode) return [];
      sectionMap[courseIdentity(rawCode)] = fromTypedSections(item.sections);
      return [{
        rawCode,
        code: formatMetuCourseCode(String(item.display_code ?? rawCode)),
        name: cleanCourseName(String(item.name ?? rawCode)),
        credits: Number(item.credits ?? 0),
      }];
    });
    setSectionsByCourse((current) => ({ ...current, ...sectionMap }));
    const warnings = (response.warnings ?? []).filter((warning): warning is string => typeof warning === "string");
    captureProductEvent("schedule_plan_completed", {
      result: "success",
      requested_courses: courses.length,
      returned_courses: verified.length,
      warnings: warnings.length,
      duration_seconds: (Date.now() - startedAt) / 1000,
    });
    return { courses: verified, warnings, prerequisiteRejections: response.prerequisite_rejections ?? [], cacheHit: response.cache_hit, durationMs: response.duration_ms };
  }

  async function loadRequiredCourses() {
    if (!department) {
      toast.error(t(
        "Bölüm bilgin bulunamadı. Ayarlar'dan akademik verilerini yenileyip tekrar dene.",
        "Your department is missing. Refresh your academic data in Settings and try again.",
      ));
      return;
    }
    setPlanBusy(true); setCurriculumNotice(""); setExpandedCourse(null);
    try {
      const result = await requestCurriculum([]);
      setCatalogCourses(result.courses);
      setPrerequisiteRejections(result.prerequisiteRejections);
      // Every course the curriculum names, checked now rather than when the
      // student happens to expand one: the red flags have to be visible while
      // they are choosing, not after.
      void fetchAllConstraints(result.courses);
      const warningText = result.warnings.map((warning) => localizedCurriculumWarning(warning, t)).join(" ");
      setCurriculumNotice(result.courses.length
        ? t(`Bu dönem alman gereken ${result.courses.length} ders bulundu.${warningText ? ` ${warningText}` : ""}`, `${result.courses.length} required courses were found for this term.${warningText ? ` ${warningText}` : ""}`)
        : t(`Müfredatında bu dönem açılan, henüz almadığın bir ders bulunamadı.${warningText ? ` ${warningText}` : ""}`, `No course from your curriculum that you still need is offered this term.${warningText ? ` ${warningText}` : ""}`));
    } catch (error) {
      captureRequestFailure(error, { operation: "schedule.required_courses", kind: "query" });
      toast.error(t(`Alman gereken dersler getirilemedi: ${error instanceof Error ? error.message : "Bilinmeyen hata"}`, `Required courses failed: ${error instanceof Error ? error.message : "Unknown error"}`));
    } finally { setPlanBusy(false); }
  }

  async function toggleCourse(course: CatalogCourse) {
    const identity = courseIdentity(course.rawCode);
    if (expandedCourse === identity) return setExpandedCourse(null);
    setExpandedCourse(identity);
    if (sectionsByCourse[identity]?.length) return;
    // The section fetch needs a department — either derived from a
    // seven-digit course code or from the student's own department. Without
    // one the broker returns 422, which is what happened when the student
    // clicked a course in the first few hundred milliseconds before the
    // SAIS context had resolved.
    const derived = owningDepartment(course.rawCode, department);
    if (!derived) {
      toast.error(t("Bölüm bilgisi henüz yüklenmedi, bir saniye bekle.", "Department info is still loading, wait a moment."));
      return;
    }
    setSectionsBusy(identity);
    try {
      const sections = await fetchSections(course, sectionsByCourse);
      // Not awaited: the section times are what the student opened the course
      // for, and the verdicts arrive a moment later without holding them up.
      void fetchConstraints(course);
      if (!sections.length) toast.error(t("Bu ders için ODTÜ sisteminde şube bulunamadı.", "No sections were found for this course in METU's system."));
      else if (sections.every((section) => !section.meetings.length)) toast.warning(t(`${course.code} için ${sections.length} şube var; ODTÜ gün ve saatleri henüz yayımlamamış.`, `${sections.length} sections exist for ${course.code}, but METU has not published their days and times yet.`));
    } catch (error) {
      captureRequestFailure(error, { operation: "schedule.sections", kind: "query" });
      toast.error(t(`${course.code} şubeleri alınamadı: ${error instanceof Error ? error.message : "Bilinmeyen hata"}`, `${course.code} sections failed: ${error instanceof Error ? error.message : "Unknown error"}`));
    } finally { setSectionsBusy(null); }
  }

  /**
   * Fill the timetable from the course pool.
   *
   * This used to read only the sections some earlier step happened to have
   * cached — and the plan endpoint deliberately returns none for a whole-term
   * recommendation, so pressing this straight after step 1 reported every
   * course as missing until the student had opened each one by hand. It now
   * fetches what it needs, one course at a time so a pool of fifteen does not
   * reach the campus catalog as fifteen concurrent calls. The three ways a
   * course can fail to be placed are reported separately, because "METU has
   * not published the times" and "no section fits your empty day" ask the
   * student to do completely different things.
   */
  async function generateSchedule() {
    if (!catalogCourses.length) return toast.error(t("Önce dönem derslerini getir.", "Load semester courses first."));
    if (!planning.ready) return toast.error(t("Program sunucusu henüz hazır değil.", "The planner server is still loading."));
    if (planning.saving || planning.retryable || planning.conflict) return toast.error(t("Önce bekleyen program kaydını tamamla.", "Finish the pending schedule save first."));
    setGenerateProgress({ done: 0, total: catalogCourses.length });
    try {
      const unavailable: string[] = [];
      const unpublished: string[] = [];
      // One request for everything still missing, rather than one per course
      // inside the loop below. A pool the catalog has already seen this week
      // costs a single call that touches no campus page at all.
      let known = sectionsByCourse;
      try {
        known = await fetchPoolSections(catalogCourses, known);
      } catch (error) {
        // Not fatal: the per-course path below still runs, so a failed batch
        // costs speed rather than the whole attempt.
        captureRequestFailure(error, { operation: "schedule.pool_sections", kind: "query" });
      }
      for (const [index, course] of catalogCourses.entries()) {
        setGenerateProgress({ done: index + 1, total: catalogCourses.length });
        let sections: CatalogSection[] = [];
        try {
          sections = await fetchSections(course, known);
          known = { ...known, [courseIdentity(course.rawCode)]: sections };
        } catch { unavailable.push(course.code); continue; }
        if (!sections.length) { unavailable.push(course.code); continue; }
        const timed = sections.filter((section) => section.meetings.length > 0);
        if (!timed.length) { unpublished.push(course.code); continue; }
      }

      // The fetched sections and server generated eligibility verdicts are
      // inputs to the owner service. Search, conflict checks, ranking and
      // minute arithmetic all run there, so chat and the browser consume the
      // same solver result.
      setSectionsByCourse(known);
      const staged = await submitPlanningUpdate({
        operation: "set_pool",
        pool: catalogCourses.map((course) => ({
          code: course.code,
          name: course.name,
          credits: course.credits,
          raw_code: course.rawCode,
        })),
        sections: canonicalSectionsFromLocal(known, constraints),
      });
      if (!staged) return;
      const solved = await submitPlanningUpdate({ operation: "solve" });
      if (!solved) return;
      const scheduled = new Set(solved.state.entries.filter((entry) => entry.kind === "course").map((entry) => courseIdentity(entry.code)));
      const sectionPayload = canonicalSectionsFromLocal(known, constraints);
      const restricted = catalogCourses
        .filter((course) => {
          const rows = sectionPayload[courseIdentity(course.rawCode)] ?? [];
          return rows.length > 0 && rows.every((row) => row && typeof row === "object" && (row as { eligible?: boolean }).eligible === false);
        })
        .map((course) => course.code);
      const unplaced = catalogCourses
        .filter((course) => !scheduled.has(courseIdentity(course.code)))
        .filter((course) => !unavailable.includes(course.code) && !unpublished.includes(course.code) && !restricted.includes(course.code))
        .map((course) => course.code);

      if (unavailable.length) toast.error(t(`${unavailable.join(", ")} için ODTÜ sisteminde şube bulunamadı.`, `No sections were found in METU's system for ${unavailable.join(", ")}.`));
      if (unpublished.length) toast.warning(t(`${unpublished.join(", ")} için gün ve saat ODTÜ tarafından henüz yayımlanmadı.`, `METU has not published days and times for ${unpublished.join(", ")} yet.`));
      if (restricted.length) toast.warning(t(`${restricted.join(", ")} için soyadına açık şube yok. Kısıtları yok sayarak tekrar dene.`, `No section of ${restricted.join(", ")} is open to your surname. Try again with restrictions ignored.`));
      if (unplaced.length) toast.warning(t(`${unplaced.join(", ")} için boş gün ve çakışma tercihlerine uyan şube yok.`, `No section of ${unplaced.join(", ")} fits your empty-day and conflict preferences.`));
      if (solved.state.alternatives.length > 1) {
        toast.success(t(
          `${solved.state.alternatives.length} alternatif program bulundu. Oklarla aralarında geçiş yap.`,
          `${solved.state.alternatives.length} possible schedules found. Use the arrows to switch between them.`,
        ));
      } else if (solved.state.alternatives.length === 1 && !unavailable.length && !unpublished.length && !unplaced.length) {
        toast.success(t("Tek bir çakışmasız program mümkün.", "Exactly one conflict-free schedule is possible."));
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("Program oluşturulamadı.", "The schedule could not be generated."));
    } finally { setGenerateProgress(null); }
  }

  async function handleUndo() {
    if (planning.saving || planning.retryable || planning.conflict || !planning.envelope?.can_undo) return;
    const submitted: SubmittedMutation = { term, fingerprint: localFingerprintRef.current, idempotencyKey: crypto.randomUUID() };
    saveTracker.current.submit(submitted.idempotencyKey, submitted.term, submitted.fingerprint);
    const next = await planning.undo(submitted.idempotencyKey);
    if (!next) return;
    reconcileSubmittedMutation(submitted, next);
  }

  async function handleImportLegacy() {
    if (!planning.legacyDraft || planning.saving || planning.retryable || planning.conflict) return;
    const submitted: SubmittedMutation = { term, fingerprint: localFingerprintRef.current, idempotencyKey: crypto.randomUUID() };
    saveTracker.current.submit(submitted.idempotencyKey, submitted.term, submitted.fingerprint);
    const next = await planning.importLegacy(submitted.idempotencyKey);
    if (!next) return;
    reconcileSubmittedMutation(submitted, next);
  }

  async function handleRestoreRecovery() {
    if (!planning.recoveryDraft || planning.saving || planning.retryable || planning.conflict) return;
    const submitted: SubmittedMutation = { term, fingerprint: localFingerprintRef.current, idempotencyKey: crypto.randomUUID() };
    saveTracker.current.submit(submitted.idempotencyKey, submitted.term, submitted.fingerprint);
    const next = await planning.restoreRecovery(submitted.idempotencyKey);
    if (!next) return;
    reconcileSubmittedMutation(submitted, next);
  }

  async function handleRefreshAfterConflict() {
    // A failed command belongs to the old base revision. A fresh read must be
    // treated as an explicit replacement, so it cannot consume that marker.
    saveTracker.current.reset(term);
    await planning.refreshAfterConflict();
  }

  function showAlternative(index: number) {
    if (alternatives.length < 2) return;
    const next = (index + alternatives.length) % alternatives.length;
    setAlternativeIndex(next);
    setEntries(alternatives[next]);
  }

  function addCatalogSection(course: CatalogCourse, section: CatalogSection) {
    if (!section.meetings.length) return toast.warning(t("Bu şubenin gün ve saati ODTÜ tarafından henüz yayımlanmamış.", "METU has not published this section's day and time yet."));
    // Eligibility is checked by the canonical owner when this state is saved.
    // The browser may show the catalog verdict, but it cannot make the
    // registration decision that chat and other clients must also observe.
    const additions = section.meetings.map((meeting, index) => ({ id: crypto.randomUUID(), code: course.code, name: course.name, section: section.section, credits: index === 0 ? course.credits : 0, color: uniqueCourses % COLORS.length, kind: "course" as const, instructor: section.instructor, ...meeting }));
    if (avoidConflicts && additions.some((next) => entries.some((entry) => overlaps(entry, next)))) return toast.error(t("Bu şube mevcut programla çakışıyor.", "This section conflicts with your schedule."));
    setEntries((current) => [...current, ...additions]);
    toast.success(t(`${course.code} şube ${section.section} eklendi.`, `${course.code} section ${section.section} added.`));
  }

  // --- sharing and export -------------------------------------------------

  function favorite() {
    if (!entries.length) return;
    const next = [...favorites, entries].slice(-10);
    setFavorites(next); setFavoriteIndex(next.length - 1);
    toast.success(t("Program favorilere eklendi.", "Schedule added to favorites."));
  }

  function nextFavorite() {
    if (!favorites.length) return;
    // Tracked by index rather than by comparing the current entries against
    // each favorite: entries are replaced wholesale by every other action, so
    // an identity comparison never matched and this always showed the first.
    const index = (favoriteIndex + 1) % favorites.length;
    setFavoriteIndex(index);
    setEntries(favorites[index] ?? []);
  }

  async function copySummary() {
    const summary = DAYS.map((day) => `${dayLabel(day)}: ${entries.filter((e) => e.day === day).sort((a, b) => itemStartMinute(a) - itemStartMinute(b)).map((e) => `${formatItemTime(e)} ${e.code}-${e.section} (${e.kind === "course" ? (e.room?.trim() || "TBA") : (e.room?.trim() || "—")})`).join(", ") || "—"}`).join("\n");
    await navigator.clipboard.writeText(summary); toast.success(t("Program özeti kopyalandı.", "Schedule summary copied."));
  }

  function exportCsv() {
    if (!entries.length) return toast.error(t("Dışa aktarılacak ders yok.", "There is nothing to export yet."));
    const header = [t("Gün", "Day"), t("Başlangıç", "Start"), t("Bitiş", "End"), t("Kod", "Code"), t("Ders adı", "Course name"), t("Şube", "Section"), t("Öğretim elemanı", "Instructor"), t("Derslik", "Room"), t("Kredi", "Credits")];
    const rows = [...entries]
      .sort((a, b) => DAYS.indexOf(a.day) - DAYS.indexOf(b.day) || itemStartMinute(a) - itemStartMinute(b))
      .map((entry) => [dayLabel(entry.day), formatItemTime(entry), formatClock(itemStartMinute(entry) + itemDurationMinutes(entry)), entry.code, entry.name, entry.section, entry.instructor ?? "", entry.kind === "course" ? (entry.room?.trim() || "TBA") : entry.room, entry.credits]);
    const csv = [header, ...rows].map((row) => row.map(csvCell).join(";")).join("\r\n");
    downloadFile(`devrimo-${term}.csv`, new Blob([`﻿${csv}`], { type: "text/csv;charset=utf-8" }));
    toast.success(t("Program CSV olarak indirildi.", "Schedule downloaded as CSV."));
  }

  function exportWallpaper() {
    const width = 3840, height = 2160;
    const baseMinute = DEFAULT_HOURS[0] * 60 + 40;
    const pixelsPerMinute = 170 / 60;
    const cells = entries.map((e) => {
      const x = 460 + DAYS.indexOf(e.day) * 650;
      const y = 330 + (itemStartMinute(e) - baseMinute) * pixelsPerMinute;
      const height = Math.max(72, itemDurationMinutes(e) * pixelsPerMinute);
      const labelY = y + Math.min(60, Math.max(34, height - 18));
      return `<rect x="${x}" y="${y}" width="610" height="${height}" rx="24" fill="#e31837" opacity=".9"/><text x="${x + 30}" y="${labelY}" fill="white" font-size="36" font-family="Arial" font-weight="700">${e.code.replace(/[<>&]/g, "")} · ${formatItemRange(e)}</text>`;
    }).join("");
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><rect width="100%" height="100%" fill="#171312"/><text x="180" y="170" fill="white" font-size="72" font-family="Arial" font-weight="700">Devrimo · ${termLabel(term, t).replace(/[<>&]/g, "")}</text>${DAYS.map((d, i) => `<text x="${500 + i * 650}" y="285" fill="#aaa" font-size="36" font-family="Arial">${dayLabel(d)}</text>`).join("")}${cells}</svg>`;
    downloadFile("devrimo-schedule-4k.svg", new Blob([svg], { type: "image/svg+xml" }));
  }

  // --- render -------------------------------------------------------------
  // Below xl the page scrolls as one column. From xl up it is pinned to the
  // viewport: the sidebar and the timetable each scroll inside themselves, so
  // the week is always fully visible without moving the page.

  return (
    <div className="h-full overflow-y-auto bg-[radial-gradient(circle_at_85%_0%,rgb(227_24_55/8%),transparent_32%)] px-4 py-4 sm:px-6 lg:px-8 xl:flex xl:flex-col xl:overflow-hidden">
      <AlertDialog open={prerequisiteRejections.length > 0} onOpenChange={(open) => { if (!open) setPrerequisiteRejections([]); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("Ön koşulu eksik dersler", "Courses with unmet prerequisites")}</AlertDialogTitle>
            <AlertDialogDescription className="space-y-2 text-left">
              {prerequisiteRejections.flatMap((rejection) => {
                const course = rejection.course_label || rejection.course_code || t("Bu ders", "This course");
                const prerequisites = rejection.prerequisite_course_labels?.length
                  ? rejection.prerequisite_course_labels
                  : rejection.prerequisite_course_codes ?? [];
                return prerequisites.map((prerequisite) => (
                  <span key={`${rejection.course_code}-${prerequisite}`} className="block">
                    {t(
                      `${course} dersini alabilmek için ${prerequisite} dersinden geçer not (DD ve üstü) almalısınız.`,
                      `To take ${course}, you must earn a passing grade (DD or higher) in ${prerequisite}.`,
                    )}
                  </span>
                ));
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("Tamam", "OK")}</AlertDialogCancel>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
      <div className="mx-auto w-full max-w-[1500px] space-y-4 xl:flex xl:min-h-0 xl:flex-1 xl:flex-col xl:gap-4 xl:space-y-0">
        <div className="flex flex-wrap items-center justify-between gap-3 xl:shrink-0">
          <div className="flex min-w-0 flex-wrap items-baseline gap-x-3">
            <h1 className="text-2xl font-semibold tracking-tight">{t("Ders programı", "Schedule")}</h1>
            <p className="truncate text-sm text-muted-foreground">{termLabel(term, t)} · {t("derslerini ekle, çakışmaları gör, paylaş", "add courses, spot conflicts, share")}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" onClick={() => setEntries([])}><RotateCcwIcon />{t("Programı temizle", "Clear schedule")}</Button>
            <Button variant="outline" size="sm" onClick={() => void handleUndo()} disabled={!planning.envelope?.can_undo || planning.saving || planning.retryable || Boolean(planning.conflict)}><RotateCcwIcon />{t("Geri al", "Undo")}</Button>
          </div>
        </div>

        {/* Above both columns: it explains the whole screen, and once dismissed
            it shrinks to a single link rather than taking space forever. */}
        <PlannerIntro className="sm:mb-4" />

        {planning.legacyDraft ? (
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm" role="status">
            <span className="min-w-0 flex-1">{t("Bu tarayıcıda eski bir program taslağı bulundu. Hesabına aktarmak ister misin?", "An older schedule draft was found in this browser. Import it into this account?")}</span>
            <Button size="sm" variant="outline" onClick={() => void handleImportLegacy()} disabled={planning.saving || planning.retryable || Boolean(planning.conflict)}>{t("Taslağı içe aktar", "Import draft")}</Button>
          </div>
        ) : null}
        {planning.recoveryDraft ? (
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm" role="status">
            <span className="min-w-0 flex-1">{t("Bu hesap için sunucu dışında kaydedilmiş bir program bulundu. Geri yüklemek ister misin?", "A confirmed recovery copy for this account was found outside the server. Restore it?")}</span>
            <Button size="sm" variant="outline" onClick={() => void handleRestoreRecovery()} disabled={planning.saving || planning.retryable || Boolean(planning.conflict)}>{t("Geri yükle", "Restore")}</Button>
          </div>
        ) : null}
        {planning.conflict ? (
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">
            <span className="min-w-0 flex-1">{t("Program başka bir sekmede değişti. Kaydetmeye devam etmek için güncel programı yükle.", "The schedule changed in another tab. Reload the current plan before saving again.")}</span>
            <Button size="sm" variant="outline" onClick={() => void handleRefreshAfterConflict()}>{t("Günceli yükle", "Reload current plan")}</Button>
          </div>
        ) : planning.saveError ? (
          <div className="flex flex-wrap items-center gap-3 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">
            <span className="min-w-0 flex-1">{planning.saveError}</span>
            {planning.retryable ? <Button size="sm" variant="outline" onClick={() => void planning.retry()} disabled={planning.saving}>{t("Tekrar dene", "Retry")}</Button> : null}
          </div>
        ) : null}

        <div className="grid gap-4 xl:min-h-0 xl:flex-1 xl:grid-cols-[340px_minmax(0,1fr)]">
          <aside className="min-w-0 space-y-4 xl:min-h-0 xl:overflow-y-auto xl:pr-1">
            <Card><CardContent className="grid grid-cols-[minmax(0,1fr)] gap-3 p-4">
              <Field id="planner-empty-days" label={t("Boş günler", "Empty days")}>
                {/* Five fixed options, so toggles rather than a multi-select:
                    every choice is visible and one click wide, and a dropdown
                    would hide the current selection behind a summary line. */}
                <div id="planner-empty-days" role="group" aria-labelledby="planner-empty-days-label" className="flex gap-1">
                  {DAYS.map((day) => {
                    const chosen = emptyDays.includes(day);
                    return (
                      <button
                        key={day}
                        type="button"
                        aria-pressed={chosen}
                        onClick={() => setEmptyDays((current) => chosen ? current.filter((item) => item !== day) : [...current, day])}
                        className={cn("h-9 flex-1 rounded-md border text-xs font-medium transition", chosen ? "border-primary bg-primary/10 text-primary" : "text-muted-foreground hover:bg-accent")}
                      >
                        {dayLabel(day)}
                      </button>
                    );
                  })}
                </div>
                {emptyDays.length === DAYS.length
                  ? <p className="mt-1 text-[11px] text-destructive">{t("Tüm günler boş seçili; hiçbir ders yerleştirilemez.", "Every day is marked empty, so nothing can be placed.")}</p>
                  : emptyDays.length ? null
                  : <p className="mt-1 text-[11px] text-muted-foreground">{t("Seçmezsen fark etmez.", "Leave empty for no preference.")}</p>}
              </Field>
              <Toggle label={t("Çakışmaları engelle", "Prevent conflicts")} checked={avoidConflicts} onChange={setAvoidConflicts} />
              <span data-tour="rules"><Toggle label={t("Şube kısıtlarını yok say", "Ignore section restrictions")} checked={ignoreConstraints} onChange={setIgnoreConstraints} /></span>
            </CardContent></Card>

            <Card data-tour="pool"><CardHeader className="pb-3"><CardTitle className="text-base">{t("Dönem dersleri", "Semester courses")}</CardTitle></CardHeader><CardContent className="grid grid-cols-[minmax(0,1fr)] gap-3">
              <Button onClick={() => void loadRequiredCourses()} disabled={busy || departmentBusy}>{planBusy ? t("Dersler belirleniyor…", "Finding courses…") : t("Almam gereken dersleri getir", "Load required courses")}</Button>
              {planBusy ? <p className="flex items-center gap-2 rounded-lg border bg-muted/30 p-2 text-xs text-muted-foreground" role="status" aria-live="polite"><Loader2Icon className="size-3.5 animate-spin" />{t("Müfredatın okunuyor…", "Reading your curriculum…")}</p> : null}
              {curriculumNotice ? <p className="rounded-lg border bg-muted/30 p-2 text-xs leading-5 text-muted-foreground">{curriculumNotice}</p> : null}
              {catalogCourses.length ? (
                <div className="flex items-center justify-between gap-3 text-xs" aria-live="polite">
                  <span className="font-medium">{t(`${catalogCourses.length} dersten ${selectedPoolCount} tanesi programa eklendi`, `${selectedPoolCount} of ${catalogCourses.length} courses added to the schedule`)}</span>
                  <span className="shrink-0 tabular-nums text-muted-foreground">{selectedPoolCount}/{catalogCourses.length}</span>
                </div>
              ) : null}
              {constraintsBusy ? <p className="flex items-center gap-2 rounded-lg border bg-muted/30 p-2 text-xs leading-5 text-muted-foreground"><Loader2Icon className="size-3.5 shrink-0 animate-spin" />{t("Şube kısıtları ODTÜ'den okunuyor; kırmızı işaretler geldikçe belirecek.", "Reading section restrictions from METU; red flags will appear as they arrive.")}</p> : null}
              <div className="space-y-2" data-tour="search">
                <div className="relative">
                  <SearchIcon className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    value={poolQuery}
                    onChange={(e) => setPoolQuery(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter" && suggestions.length) addPoolCourse(); }}
                    className="h-11 pl-9 pr-12"
                    placeholder={t("Ders kodu veya adı ara… (PHYS213, termodinamik)", "Search by code or name… (PHYS213, thermodynamics)")}
                    aria-label={t("Ders kodu veya adı ara", "Search by course code or name")}
                  />
                  {suggestBusy
                    ? <Loader2Icon className="absolute right-3 top-1/2 size-4 -translate-y-1/2 animate-spin text-muted-foreground" />
                    : <Button size="icon" variant="ghost" onClick={() => addPoolCourse()} className="absolute right-1 top-1/2 -translate-y-1/2" aria-label={t("Dersi havuza ekle", "Add course to pool")}><PlusIcon /></Button>}
                </div>
                {suggestions.length ? <ul className="max-h-64 divide-y overflow-y-auto rounded-xl border bg-background" role="listbox" aria-label={t("Ders önerileri", "Course suggestions")}>
                  {suggestions.map((item) => (
                    <li key={item.rawCode}>
                      <button
                        type="button"
                        role="option"
                        aria-selected={false}
                        onClick={() => addPoolCourse(item)}
                        className="flex w-full items-start gap-2 p-2 text-left text-sm transition hover:bg-primary/5"
                      >
                        <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-xs font-semibold">{item.code}</span>
                        <span className="min-w-0 flex-1 break-words text-xs leading-snug text-muted-foreground">{localizedCourseName(item.name, locale)}</span>
                        {item.credits ? <span className="shrink-0 text-[11px] text-muted-foreground">{item.credits} {t("kr", "cr")}</span> : null}
                      </button>
                    </li>
                  ))}
                </ul> : null}
                {!suggestions.length && suggestNote ? <p className="rounded-lg border bg-muted/30 p-2 text-xs leading-5 text-muted-foreground">{suggestNote}</p> : null}
              </div>
              {catalogCourses.length ? <>
                <div className="flex items-center gap-2">
                  <Input value={catalogSearch} onChange={(e) => setCatalogSearch(e.target.value)} placeholder={t("Eklenen derslerde ara", "Search added courses")} />
                  <Button variant="outline" className="shrink-0 text-destructive" onClick={clearCoursePool}><Trash2Icon />{t("Tümünü sil", "Clear all")}</Button>
                </div>
                <div className="space-y-1">
                  {visibleCourses.map((course) => {
                    const identity = courseIdentity(course.rawCode);
                    const expanded = expandedCourse === identity;
                    const sections = sectionsByCourse[identity] ?? [];
                    return (
                      <div key={course.rawCode} className={cn("overflow-hidden rounded-lg border transition", expanded && "border-primary bg-primary/5")}>
                        <div className="flex items-center gap-1 p-1">
                          <button onClick={() => void toggleCourse(course)} aria-expanded={expanded} className="flex min-w-0 flex-1 items-center gap-2 p-1 text-left text-sm">
                            <span className="min-w-0 flex-1"><span className="block font-semibold">{course.code}</span><span className="block line-clamp-2 text-xs text-muted-foreground">{localizedCourseName(course.name, locale)}</span></span>
                            <ChevronDownIcon className={cn("size-4 shrink-0 transition-transform", expanded && "rotate-180")} />
                          </button>
                          <Button size="icon" variant="ghost" aria-label={t(`${course.code} dersini havuzdan çıkar`, `Remove ${course.code} from course pool`)} onClick={() => removePoolCourse(course)}><Trash2Icon /></Button>
                        </div>
                        {expanded ? <div className="space-y-2 border-t p-2">
                          {sectionsBusy === identity ? <p className="flex items-center gap-2 text-xs text-muted-foreground"><Loader2Icon className="size-3.5 animate-spin" />{t("Şubeler getiriliyor…", "Loading sections…")}</p>
                            : sections.length ? sections.map((section) => {
                              const check = sectionAllowed(course, section);
                              const eligible = check.allowed;
                              const reason = localizedRestrictionReason(check.reason, t);
                              const closedLabel = check.reason
                                ? t(`Bu şube sana kapalı: ${reason}.`, `This section is closed to you: ${reason}.`)
                                : t("Bu şubenin kısıtlarına uymuyorsun.", "You do not meet this section's restrictions.");
                              return (
                                <button
                                  key={section.section}
                                  onClick={() => addCatalogSection(course, section)}
                                  className={cn("w-full rounded-lg border bg-background p-2 text-left text-sm transition hover:border-primary/40 hover:bg-primary/5", !eligible && !ignoreConstraints && "opacity-60")}
                                >
                                  <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                                    <span className="font-semibold">{t("Şube", "Section")} {section.section}</span>
                                  </span>
                                  {!eligible ? <span className="mt-1 flex items-start gap-1.5 rounded-md bg-destructive/10 px-2 py-1.5 text-xs leading-snug text-destructive"><TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />{closedLabel}</span> : null}
                                  {section.instructor ? <span className="mt-0.5 block break-words text-xs font-medium text-foreground/80">{section.instructor}</span> : null}
                                  <span className="mt-1 block break-words text-xs text-muted-foreground">{section.meetings.length ? section.meetings.map((meeting) => `${dayLabel(meeting.day)} ${formatItemRange(meeting)} · ${meeting.room?.trim() || "TBA"}`).join(" / ") : t("Gün ve saat henüz yayımlanmadı", "Day and time not published yet")}</span>
                                  {/* Shown as returned by the server. The
                                      department's free-text note remains
                                      context; eligibility is a server verdict. */}
                                  {section.constraint ? <span className="mt-1 block rounded bg-muted/50 px-1.5 py-1 text-[11px] leading-snug text-muted-foreground">{localizedRestrictionReason(section.constraint, t)}</span> : null}
                                </button>
                              );
                            }) : <p className="text-xs text-muted-foreground">{t("Bu ders için şube bulunamadı.", "No sections found for this course.")}</p>}
                        </div> : null}
                      </div>
                    );
                  })}
                </div>
              </> : null}
            </CardContent></Card>

            {entries.length ? <Card><CardHeader className="pb-3"><CardTitle className="text-base">{t("Eklenen dersler", "Added courses")}</CardTitle></CardHeader><CardContent className="space-y-2">
              {scheduledCourseGroups.map((courseEntries) => { const entry = courseEntries[0]; return (
                <div key={`${entry.code}-${entry.section}`} className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2">
                  <div className="min-w-0"><p className="line-clamp-2 text-sm font-medium">{entry.code} · {localizedCourseName(entry.name, locale)}</p><p className="text-xs text-muted-foreground">{courseEntries.map((meeting) => `${dayLabel(meeting.day)} ${formatItemRange(meeting)}`).join(" / ")} · {t("Şube", "Section")} {entry.section}{entry.instructor ? ` · ${entry.instructor}` : ""}</p></div>
                  <Button size="icon" variant="ghost" aria-label={t("Dersi kaldır", "Remove course")} onClick={() => removeScheduledCourse(courseEntries)}><Trash2Icon /></Button>
                </div>
              ); })}
            </CardContent></Card> : null}

            <Card data-tour="manual">
              <button onClick={() => setManualOpen((open) => !open)} aria-expanded={manualOpen} className="flex w-full items-center gap-2 px-4 py-3 text-left">
                <span className="min-w-0 flex-1 text-base font-semibold">{t("Elle ders veya blok ekle", "Add course or block manually")}</span>
                <ChevronDownIcon className={cn("size-4 shrink-0 text-muted-foreground transition-transform", manualOpen && "rotate-180")} />
              </button>
              {manualOpen ? <CardContent className="grid grid-cols-[minmax(0,1fr)] gap-3 border-t pt-4">
                <div className="grid grid-cols-2 gap-2"><Field id="planner-course-code" label={t("Kod", "Code")}><Input id="planner-course-code" value={draft.code} onChange={(e) => setDraft({ ...draft, code: e.target.value })} placeholder="MATH 260" /></Field><Field id="planner-section" label={t("Şube", "Section")}><Input id="planner-section" value={draft.section} onChange={(e) => setDraft({ ...draft, section: e.target.value })} /></Field></div>
                <Field id="planner-course-name" label={t("Ders adı", "Course name")}><Input id="planner-course-name" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder={t("Temel Lineer Cebir", "Basic Linear Algebra")} /></Field>
                <div className="grid grid-cols-3 gap-2"><Field id="planner-day" label={t("Gün", "Day")}><select id="planner-day" value={draft.day} onChange={(e) => setDraft({ ...draft, day: e.target.value as Day })} className="h-10 w-full rounded-md border bg-background px-2 text-sm">{DAYS.map((d) => <option key={d} value={d}>{dayLabel(d)}</option>)}</select></Field><Field id="planner-start" label={t("Başlangıç", "Start")}><select id="planner-start" value={draft.start} onChange={(e) => setDraft({ ...draft, start: Number(e.target.value) })} className="h-10 w-full rounded-md border bg-background px-2 text-sm">{hours.map((h) => <option key={h} value={h}>{formatClock(h * 60 + 40)}</option>)}</select></Field><Field id="planner-duration" label={t("Süre", "Hours")}><select id="planner-duration" value={draft.duration} onChange={(e) => setDraft({ ...draft, duration: Number(e.target.value) })} className="h-10 w-full rounded-md border bg-background px-2 text-sm">{[1, 2, 3].map((h) => <option key={h}>{h}</option>)}</select></Field></div>
                <div className="grid grid-cols-2 gap-2"><Field id="planner-room" label={t("Derslik", "Room")}><Input id="planner-room" value={draft.room} onChange={(e) => setDraft({ ...draft, room: e.target.value })} placeholder="M-13" /></Field><Field id="planner-credits" label={t("Kredi", "Credits")}><Input id="planner-credits" type="number" min={0} max={10} value={draft.credits} onChange={(e) => setDraft({ ...draft, credits: Number(e.target.value) })} /></Field></div>
                <Button onClick={() => void addEntry()} disabled={manualBusy}>{manualBusy ? <Loader2Icon className="animate-spin" /> : <PlusIcon />}{t("Programa ekle", "Add to schedule")}</Button>
              </CardContent> : null}
            </Card>
          </aside>

          {/* min-w-0 is load-bearing: the timetable carries a min-width so its
              five day columns stay legible, and without this the single grid
              column below xl grows to that width and takes the whole page
              sideways with it. */}
          <section aria-label={t("Haftalık program", "Weekly schedule")} className="min-w-0 space-y-3 xl:flex xl:min-h-0 xl:flex-col xl:gap-3 xl:space-y-0">
            <Card className="overflow-hidden xl:flex xl:min-h-0 xl:flex-1 xl:flex-col">
              {/* Deliberately not CardHeader: that is a grid with auto rows, so
                  a title, a stat line and two icon buttons became three stacked
                  rows and about eighty pixels of empty card above the week.
                  One flex row instead — everything on the same line, and the
                  height goes back to the timetable. */}
              <div className="flex items-center gap-x-3 gap-y-1 px-4 py-2 xl:shrink-0">
                <CardTitle className="shrink-0 text-sm">{termLabel(term, t)}</CardTitle>
                <p className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
                  <span className="font-medium text-foreground">{totalCredits}</span> {t("kredi", "credits")} · <span className="font-medium text-foreground">{totalHours}</span> {t("saat", "hours")} · <span className="font-medium text-foreground">{uniqueCourses}</span> {t("ders", "courses")}
                  {conflicts.size ? <span className="font-medium text-destructive"> · {t(`${conflicts.size} çakışma`, `${conflicts.size} conflicts`)}</span> : null}
                </p>
                <div className="flex shrink-0 gap-0.5">
                  <Button size="icon-sm" variant="ghost" aria-label={t("Favoriye ekle", "Favorite")} onClick={favorite}><HeartIcon /></Button>
                  <Button size="icon-sm" variant="ghost" aria-label={t("Özeti kopyala", "Copy summary")} onClick={() => void copySummary()}><ClipboardIcon /></Button>
                </div>
              </div>
              <CardContent className="p-0 xl:min-h-0 xl:flex-1" data-tour="grid">
                <div className="border-t p-3 sm:hidden">
                  <div className="mb-3 grid grid-cols-5 gap-1" role="tablist" aria-label={t("Program günü", "Schedule day")}>
                    {DAYS.map((day) => (
                      <button
                        key={day}
                        type="button"
                        role="tab"
                        aria-selected={mobileDay === day}
                        onClick={() => setMobileDay(day)}
                        className={cn("h-9 rounded-md border text-xs font-medium transition", mobileDay === day ? "border-primary bg-primary text-primary-foreground" : "text-muted-foreground")}
                      >
                        {dayLabel(day)}
                      </button>
                    ))}
                  </div>
                  <div className="space-y-2" role="tabpanel" aria-label={dayLabel(mobileDay)}>
                    {mobileEntries.length ? mobileEntries.map((entry) => (
                      <button
                        key={entry.id}
                        type="button"
                        onClick={() => setEntries((current) => current.filter((item) =>
                          entry.kind === "block"
                            ? item.id !== entry.id
                            : !(item.kind === "course" && item.code === entry.code && item.section === entry.section)))}
                        className={cn("flex w-full items-start gap-3 rounded-xl border-l-4 p-3 text-left", COLORS[entry.color % COLORS.length], conflicts.has(entry.id) && "ring-2 ring-destructive")}
                        aria-label={t(`${entry.code} dersini programdan kaldır`, `Remove ${entry.code} from the schedule`)}
                      >
                        <span className="w-24 shrink-0 text-sm font-semibold tabular-nums">
                          {formatItemRange(entry)}
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block font-semibold">{entry.code}{entry.section ? ` · ${t("Şube", "Section")} ${entry.section}` : ""}</span>
                          <span className="mt-0.5 block text-xs leading-snug opacity-85">{localizedCourseName(entry.name, locale)}</span>
                          <span className="mt-1 block text-xs opacity-75">{entry.room?.trim() || (entry.kind === "course" ? "TBA" : "—")}{entry.instructor ? ` · ${entry.instructor}` : ""}</span>
                        </span>
                      </button>
                    )) : <p className="rounded-xl border border-dashed p-5 text-center text-sm text-muted-foreground">{t(`${dayLabel(mobileDay)} günü ders yok.`, `No classes on ${dayLabel(mobileDay)}.`)}</p>}
                  </div>
                </div>
                <div className="hidden overflow-auto sm:block xl:h-full">
                  <div
                  className="grid min-w-[820px] grid-cols-[68px_repeat(5,minmax(130px,1fr))] border-t text-sm xl:h-full"
                  style={{ gridTemplateRows: `auto repeat(${hours.length}, minmax(${ROW_MIN_PX}px, 1fr))` }}
                  >
                  {/* No bottom border: the 08:40 label sits on this line, and a
                      rule through the text is the thing being removed. Sticky
                      with the day names, or the corner slides under them. */}
                  <div className="sticky top-0 z-30 border-r bg-card" />
                  {DAYS.map((d) => <div key={d} className={cn("sticky top-0 z-30 border-b border-r bg-card px-2 py-2 text-center text-sm font-semibold", emptyDays.includes(d) && "bg-primary/10 text-primary")}>{dayLabel(d)}</div>)}
                  {hours.flatMap((hour) => [
                    // One time per line, sitting on the line it names. Writing
                    // both ends inside every row said each boundary twice —
                    // 09:40 closing one row and opening the next — which is the
                    // clutter, and it still left the reader matching a label to
                    // a line. This column carries no bottom border, so the rules
                    // begin after the text instead of running through it.
                    <div key={`h-${hour}`} className="relative border-r bg-muted/30">
                      {/* Centred on the rule it names, except the first: that
                          rule is the sticky day-header's own bottom edge, and a
                          label straddling it is half-hidden behind the header. */}
                      <span className={cn("absolute right-2 text-[11px] font-medium leading-none tabular-nums text-muted-foreground", hour === hours[0] ? "top-1.5" : "top-0 -translate-y-1/2")}>
                        {formatClock(hour * 60 + 40)}
                      </span>
                      {hour === hours[hours.length - 1] ? (
                        <span className="absolute bottom-0 right-2 translate-y-1/2 text-[11px] font-medium leading-none tabular-nums text-muted-foreground">
                          {formatClock((hour + 1) * 60 + 40)}
                        </span>
                      ) : null}
                    </div>,
                    ...DAYS.map((day) => {
                      const here = entries.filter((e) => e.day === day && e.start === hour);
                      return (
                        <div key={`${day}-${hour}`} className={cn("relative border-b border-r", emptyDays.includes(day) && "bg-muted/20")}>
                          {here.map((entry) => {
                            const { lane, lanes } = gridLanes.get(entry.id) ?? { lane: 0, lanes: 1 };
                            return (
                            <button
                              key={entry.id}
                              // A course is taken as a whole section, not as
                              // individual hours: removing Monday's lecture and
                              // leaving Wednesday's behind describes a
                              // registration that cannot exist. Blocks are the
                              // exception — those are the student's own
                              // one-off entries.
                              onClick={() => setEntries((current) => current.filter((item) =>
                                entry.kind === "block"
                                  ? item.id !== entry.id
                                  : !(item.kind === "course" && item.code === entry.code && item.section === entry.section)))}
                              title={`${entry.code} · ${entry.name}${entry.section ? ` · ${t("Şube", "Section")} ${entry.section}` : ""} · ${entry.room?.trim() || "TBA"}${entry.instructor ? ` · ${entry.instructor}` : ""}
${entry.kind === "block" ? t("Kaldırmak için tıkla", "Click to remove") : t("Dersi tüm saatleriyle kaldırmak için tıkla", "Click to remove the course and all its hours")}`}
                              style={{
                                // Absolute rather than in flow, so the ten hour
                                // rows can share the available height and the
                                // week fits the viewport. 100% is this cell; a
                                // multi-hour session also covers the 1px
                                // borders between the cells it spans. Several
                                // sessions starting in one cell split its width
                                // into lanes instead of hiding one another.
                                // Flush to the top of its row, so the block's
                                // edge, the rule and the time label all sit on
                                // the same line. It used to start four pixels
                                // below, which is small enough to look like a
                                // mistake and large enough that you could not
                                // tell which hour a session belonged to.
                                // Exactly this many rows tall. The percentage
                                // is the row, now that nothing forces the cell
                                // to a different height, so a two-hour session
                                // covers two rows however tall the card is.
                                // Filling the cell edge to edge, the way a
                                // merged cell does in a spreadsheet, so the
                                // rules above and below are the session's own
                                // edges and its hours can simply be counted.
                                height: `calc(${entry.duration} * 100% + ${entry.duration - 1}px)`,
                                width: `calc(100% / ${lanes})`,
                                left: `calc(${lane} * 100% / ${lanes})`,
                              }}
                              className={cn("absolute top-0 z-10 overflow-hidden border-b border-l-4 border-r px-1.5 py-1 text-left leading-tight transition hover:brightness-110", COLORS[entry.color % COLORS.length], conflicts.has(entry.id) && "z-20 ring-2 ring-inset ring-destructive")}
                            >
                              {/* The room shares the first line rather than
                                  taking a third one. A one-hour block is two
                                  lines tall, so a third was simply clipped and
                                  exactly the sessions most likely to be
                                  somewhere unexpected showed no room at all. */}
                              <span className="flex items-baseline gap-1.5">
                                <span className="min-w-0 flex-1 truncate text-[13px] font-semibold">{entry.code} · {entry.section}</span>
                                {entry.kind === "course" || entry.room?.trim() ? (
                                  <span className="shrink-0 text-[11px] font-medium opacity-80">{entry.room?.trim() || "TBA"}</span>
                                ) : null}
                              </span>
                              <span className="mt-0.5 block truncate text-xs opacity-85">{localizedCourseName(entry.name, locale)}</span>
                            </button>
                            );
                          })}
                        </div>
                      );
                    }),
                  ])}
                  </div>
                </div>
              </CardContent>
            </Card>

            <div className="flex flex-wrap gap-2 xl:shrink-0 [&_button]:h-9">
              <Button data-tour="build" onClick={() => void generateSchedule()} disabled={busy}>{generateProgress ? t(`Program oluşturuluyor… (${generateProgress.done}/${generateProgress.total})`, `Generating… (${generateProgress.done}/${generateProgress.total})`) : t("Programı oluştur", "Generate schedule")}</Button>
              {alternatives.length > 1 ? (
                <div className="flex min-w-0 max-w-full items-center gap-0.5 rounded-md border bg-background px-0.5">
                  <Button size="icon" variant="ghost" className="size-8" aria-label={t("Önceki alternatif", "Previous option")} onClick={() => showAlternative(alternativeIndex - 1)}><ChevronLeftIcon /></Button>
                  <span className="min-w-0 truncate px-1 text-xs tabular-nums" aria-live="polite">
                    {t(`Alternatif ${alternativeIndex + 1}/${alternatives.length}`, `Option ${alternativeIndex + 1}/${alternatives.length}`)}
                    {currentShape ? <span className="ml-1.5 text-muted-foreground">{currentShape.freeDays.length ? t(`· boş: ${currentShape.freeDays.map(dayLabel).join(", ")}`, `· free: ${currentShape.freeDays.map(dayLabel).join(", ")}`) : t("· boş gün yok", "· no free day")}</span> : null}
                  </span>
                  <Button size="icon" variant="ghost" className="size-8" aria-label={t("Sonraki alternatif", "Next option")} onClick={() => showAlternative(alternativeIndex + 1)}><ChevronRightIcon /></Button>
                </div>
              ) : null}
              <Button variant="outline" onClick={exportCsv}><DownloadIcon />{t("CSV olarak indir", "Download as CSV")}</Button>
              <Button variant="outline" onClick={exportWallpaper}><DownloadIcon />{t("4K duvar kâğıdı", "4K wallpaper")}</Button>
              {favorites.length ? <Button variant="ghost" onClick={nextFavorite}>{t("Sonraki favori", "Next favorite")}</Button> : null}
            </div>
            <div data-tour="assistant"><PlannerAssistant /></div>
          </section>
        </div>
      </div>
    </div>
  );
}

function Field({ id, label, children }: { id: string; label: string; children: React.ReactNode }) { return <div className="space-y-1.5"><Label id={`${id}-label`} htmlFor={id}>{label}</Label>{children}</div>; }
function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (value: boolean) => void }) { return <div className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2"><Label className="leading-5">{label}</Label><Switch aria-label={label} checked={checked} onCheckedChange={onChange} /></div>; }
