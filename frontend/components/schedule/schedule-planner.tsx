"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDownIcon, ChevronLeftIcon, ChevronRightIcon, ClipboardIcon, DownloadIcon, HeartIcon,
  Loader2Icon, PlusIcon, RotateCcwIcon, SearchIcon, Trash2Icon, TriangleAlertIcon,
} from "lucide-react";
import { toast } from "sonner";
import { useLocale } from "@/components/locale-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { captureProductEvent, captureRequestFailure } from "@/components/posthog-analytics";
import { jsonFetch } from "@/lib/api/fetcher";
import { PlannerAssistant } from "@/components/schedule/planner-assistant";
import { PlannerIntro } from "@/components/schedule/planner-intro";

type Day = "Mon" | "Tue" | "Wed" | "Thu" | "Fri";
// `instructor` is optional because plans saved before it existed are still in
// students' browsers and must keep loading.
type Entry = { id: string; code: string; name: string; section: string; day: Day; start: number; duration: number; room: string; credits: number; color: number; kind: "course" | "block"; instructor?: string };
type CatalogCourse = { code: string; name: string; credits: number; rawCode: string };
type SurnameRange = { from: string; to: string };
type CatalogSection = { section: string; instructor: string; meetings: { day: Day; start: number; duration: number; room: string }[]; constraint: string };
type SectionMap = Record<string, CatalogSection[]>;
// Keyed by course identity, then by section number.
type ConstraintRow = { given_dept?: string; start_char?: string; end_char?: string; min_cgpa?: string; max_cgpa?: string; min_year?: string; max_year?: string };
type SectionVerdict = { rows: ConstraintRow[]; eligible: boolean; reason: string };
type ConstraintMap = Record<string, Record<string, SectionVerdict>>;
type DepartmentOption = { code: string; name: string };
type SavedPlan = { entries: Entry[]; department: string; departmentLabel: string; emptyDays: Day[]; avoidConflicts: boolean; ignoreConstraints: boolean; pool: CatalogCourse[]; sections: SectionMap; alternatives: Entry[][]; alternativeIndex: number };
type AiPlanCourse = { code?: string; name?: string; credits?: number; sections?: unknown };

const DAYS: Day[] = ["Mon", "Tue", "Wed", "Thu", "Fri"];
const HOURS = Array.from({ length: 10 }, (_, index) => index + 8);
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
const STORAGE_KEY = "devrimo:schedule:v1";
const PLAN_KEY = `${STORAGE_KEY}:plan`;

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

const keyValue = (record: Record<string, unknown>, candidates: string[]) => {
  const key = Object.keys(record).find((item) => candidates.some((candidate) => item.toLowerCase().replace(/[^a-z0-9]/g, "").includes(candidate)));
  return key ? record[key] : undefined;
};

function sectionRecords(value: unknown): Record<string, unknown>[] {
  if (Array.isArray(value)) return value.flatMap(sectionRecords);
  if (!value || typeof value !== "object") return [];
  const record = value as Record<string, unknown>;
  const normalizedKeys = Object.keys(record).map((key) => key.toLowerCase().replace(/[^a-z0-9]/g, ""));
  const isSectionRow = normalizedKeys.some((key) => ["sectionnumber", "section", "sectionno", "sec"].includes(key));
  const hasMeetingData = normalizedKeys.some((key) => ["schedule", "meetings", "meeting", "lecturehours", "hours", "time"].includes(key));
  if (isSectionRow || hasMeetingData) return [record];
  return Object.values(record).flatMap(sectionRecords);
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

// The first three digits of a seven-digit code name the department that owns
// the course. Anything shorter does not say, and the backend resolves it
// against the catalog rather than assuming the student's own department.
function owningDepartment(courseCode: string, fallback: string) {
  const digits = courseCode.replace(/\D/g, "");
  return digits.length === 7 ? digits.slice(0, 3) : fallback;
}

// METU letters, in the order Turkish collation puts them. Ranges in a section
// note are written against this alphabet, so "Ç" falls inside A-D and "I"
// inside H-J — which `localeCompare(…, "tr")` gets right and a byte or an
// English-locale comparison does not.
const TR_LETTER = "A-Za-zÇĞİıÖŞÜçğöşü";

/**
 * The surname range a section is restricted to, when it states one.
 *
 * `critical_info` is a free-text box a department types into, so this only
 * claims a range it is confident about: either the note says surname, or the
 * whole note *is* a range. A bare "A-K" buried in a longer sentence is left
 * alone — guessing wrong here silently hides sections a student can take.
 */
function parseSurnameRange(text: string): SurnameRange | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const named = /soyad|soyisim|surname|last\s*name/i.test(trimmed);
  const bare = new RegExp(`^[\\s(\\[]*[${TR_LETTER}]\\s*[-–—]\\s*[${TR_LETTER}][\\s)\\].]*$`).test(trimmed);
  if (!named && !bare) return null;
  const match = trimmed.match(new RegExp(`([${TR_LETTER}])\\s*(?:[-–—]|\\.\\.|to|ile)\\s*([${TR_LETTER}])`));
  if (!match) return null;
  return { from: match[1].toLocaleUpperCase("tr"), to: match[2].toLocaleUpperCase("tr") };
}

/** Whether this section's stated surname range admits this student. */
function surnameEligible(section: CatalogSection, surname: string) {
  const range = parseSurnameRange(section.constraint ?? "");
  if (!range) return true;
  const initial = surname.trim().charAt(0).toLocaleUpperCase("tr");
  // An unknown surname excludes nobody: the student simply has not told us.
  if (!initial) return true;
  return initial.localeCompare(range.from, "tr") >= 0 && initial.localeCompare(range.to, "tr") <= 0;
}

function parseDay(value: string): Day | null {
  const text = value.toLowerCase();
  if (/monday|pazartesi|\bmon\b/.test(text)) return "Mon";
  if (/tuesday|salı|sali|\btue\b/.test(text)) return "Tue";
  if (/wednesday|çarşamba|carsamba|\bwed\b/.test(text)) return "Wed";
  if (/thursday|perşembe|persembe|\bthu\b/.test(text)) return "Thu";
  if (/friday|cuma|\bfri\b/.test(text)) return "Fri";
  return null;
}

function parseSections(value: unknown): CatalogSection[] {
  if (typeof value === "string") {
    try { return parseSections(JSON.parse(value)); } catch {
      const chunks = value.split(/(?=^\s*(?:section|şube|sube)\s*(?:no\.?\s*)?[:#-]?\s*\d+)/gim);
      return chunks.flatMap((chunk, index) => {
        const section = chunk.match(/(?:section|şube|sube)\s*(?:no\.?\s*)?[:#-]?\s*(\d+)/i)?.[1] ?? String(index + 1);
        const instructor = chunk.match(/(?:instructor|lecturer|öğretim elemanı|ogretim elemani)\s*[:|-]\s*([^\n|]+)/i)?.[1]?.trim() ?? "";
        const room = chunk.match(/(?:room|classroom|derslik)\s*[:|-]\s*([^\n|]+)/i)?.[1]?.trim() ?? "";
        const meetings = [...chunk.matchAll(/(monday|tuesday|wednesday|thursday|friday|pazartesi|salı|sali|çarşamba|carsamba|perşembe|persembe|cuma|mon|tue|wed|thu|fri)[^\n\d]*(\d{1,2})[:.]?(\d{2})\s*[-–]\s*(\d{1,2})[:.]?(\d{2})/gi)].flatMap((match) => {
          const day = parseDay(match[1]);
          if (!day) return [];
          const startMinutes = Number(match[2]) * 60 + Number(match[3]);
          const endMinutes = Number(match[4]) * 60 + Number(match[5]);
          return [{ day, start: Math.floor(startMinutes / 60), duration: Math.max(1, Math.ceil((endMinutes - startMinutes) / 60)), room }];
        });
        const constraint = chunk.match(/(?:critical|kısıt|kisit|ek bilgi|not)\s*[:|-]\s*([^\n|]+)/i)?.[1]?.trim() ?? "";
        return [{ section, instructor, meetings, constraint }];
      });
    }
  }
  return sectionRecords(value).flatMap((record, index) => {
    const section = String(keyValue(record, ["sectionnumber", "section", "sec"]) ?? index + 1);
    const instructorValue = keyValue(record, ["instructors", "instructor", "lecturer", "teacher"]);
    const instructor = Array.isArray(instructorValue) ? instructorValue.filter(Boolean).join(", ") : String(instructorValue ?? "");
    const scheduleValue = keyValue(record, ["schedule", "meeting", "hours", "time"]);
    const room = String(keyValue(record, ["room", "classroom", "location"]) ?? "");
    const scheduleTexts = Array.isArray(scheduleValue) ? scheduleValue.map((item) => typeof item === "string" ? item : JSON.stringify(item)) : [typeof scheduleValue === "string" ? scheduleValue : JSON.stringify(scheduleValue ?? record)];
    const meetingRecords: Record<string, unknown>[] = [];
    const visitMeetings = (item: unknown) => {
      if (Array.isArray(item)) return item.forEach(visitMeetings);
      if (!item || typeof item !== "object") return;
      const candidate = item as Record<string, unknown>;
      // "time" is the shape the catalog actually answers with — {day, time:
      // "08:40-10:30", room} — and requiring a separate start key rejected all
      // of them. Every meeting then fell through to a regex over the stringified
      // record, which found the day and the hours but took the room from the
      // section instead of the meeting, where it does not exist. That is why
      // every block read TBA while the catalog was returning "B07".
      const hasDay = keyValue(candidate, ["day", "weekday", "gun"]);
      const hasWhen = keyValue(candidate, ["starttime", "start", "begin", "baslangic"])
        ?? keyValue(candidate, ["time", "hours", "saat"]);
      if (hasDay && hasWhen) meetingRecords.push(candidate);
      else Object.values(candidate).forEach(visitMeetings);
    };
    visitMeetings(scheduleValue);
    const structuredMeetings = meetingRecords.flatMap((meeting) => {
      const day = parseDay(String(keyValue(meeting, ["day", "weekday", "gun"]) ?? ""));
      const timeText = String(keyValue(meeting, ["time", "hours", "saat"]) ?? "");
      const range = timeText.match(/(\d{1,2}:\d{2})\s*(?:[-–]|to)\s*(\d{1,2}:\d{2})/i);
      const startText = String(keyValue(meeting, ["starttime", "start", "begin", "baslangic"]) ?? range?.[1] ?? "");
      const endText = String(keyValue(meeting, ["endtime", "end", "finish", "bitis"]) ?? range?.[2] ?? "");
      const startMatch = startText.match(/(\d{1,2})(?::(\d{2}))?/);
      const endMatch = endText.match(/(\d{1,2})(?::(\d{2}))?/);
      if (!day || !startMatch || !endMatch) return [];
      const startMinutes = Number(startMatch[1]) * 60 + Number(startMatch[2] ?? 0);
      const endMinutes = Number(endMatch[1]) * 60 + Number(endMatch[2] ?? 0);
      return [{ day, start: Math.floor(startMinutes / 60), duration: Math.max(1, Math.ceil((endMinutes - startMinutes) / 60)), room: String(keyValue(meeting, ["room", "classroom", "location"]) ?? room) }];
    });
    const textMeetings = scheduleTexts.flatMap((text) => {
      const day = parseDay(text);
      const times = text.match(/(\d{1,2}):\d{2}\s*(?:[-–]|to)\s*(\d{1,2}):\d{2}/i);
      if (!day || !times) return [];
      const start = Number(times[1]);
      return [{ day, start, duration: Math.max(1, Number(times[2]) - start), room }];
    });
    const meetings = structuredMeetings.length ? structuredMeetings : textMeetings;
    // METU calls this "critical info": a free-text box per section, which is
    // where departments write who the section is actually open to.
    const constraint = String(keyValue(record, ["criticalinfo", "critical", "constraint", "kisit", "eklenti"]) ?? "").trim();
    return [{ section, instructor, meetings, constraint }];
  });
}

function overlaps(a: { day: Day; start: number; duration: number }, b: { day: Day; start: number; duration: number }) {
  return a.day === b.day && a.start < b.start + b.duration && b.start < a.start + a.duration;
}

type CourseOptions = { course: CatalogCourse; options: CatalogSection[] };
type SectionChoice = { course: CatalogCourse; section: CatalogSection };

// This search is plain local backtracking — no model, no network — so the only
// real budget is the main thread. The bound on solutions is generous because
// collecting them is cheap; the bound on visited nodes is what actually keeps
// a pathological pool from freezing the tab, and it is the one that matters.
const MAX_ALTERNATIVES = 200;
const MAX_SEARCH_NODES = 1_000_000;

function sectionConflicts(section: CatalogSection, chosen: SectionChoice[]) {
  return section.meetings.some((meeting) =>
    chosen.some((item) => item.section.meetings.some((other) => overlaps(meeting, other))));
}

/** Every way to take all of these courses, up to {@link MAX_ALTERNATIVES}. */
function buildAlternatives(courses: CourseOptions[], avoidConflicts: boolean): SectionChoice[][] {
  // Fewest options first. A course with a single section constrains everything
  // after it, so deciding it early prunes far more of the tree than deciding a
  // course with eight sections early would.
  const ordered = [...courses].sort((a, b) => a.options.length - b.options.length);
  const solutions: SectionChoice[][] = [];
  const chosen: SectionChoice[] = [];
  let nodes = 0;

  const exhausted = () => solutions.length >= MAX_ALTERNATIVES || nodes >= MAX_SEARCH_NODES;

  function search(index: number) {
    if (exhausted()) return;
    if (index === ordered.length) {
      solutions.push([...chosen]);
      return;
    }
    for (const section of ordered[index].options) {
      nodes += 1;
      if (avoidConflicts && sectionConflicts(section, chosen)) continue;
      chosen.push({ course: ordered[index].course, section });
      search(index + 1);
      chosen.pop();
      if (exhausted()) return;
    }
  }

  search(0);
  return solutions;
}

/** One section per course, skipping whatever will not fit. The fallback. */
function greedyPlan(courses: CourseOptions[], avoidConflicts: boolean) {
  const chosen: SectionChoice[] = [];
  const skipped: string[] = [];
  for (const { course, options } of courses) {
    const section = options.find((candidate) => !avoidConflicts || !sectionConflicts(candidate, chosen));
    if (section) chosen.push({ course, section });
    else skipped.push(course.code);
  }
  return { chosen, skipped };
}

function choiceEntries(choices: SectionChoice[]): Entry[] {
  return choices.flatMap(({ course, section }, courseIndex) =>
    section.meetings.map((meeting, meetingIndex) => ({
      id: crypto.randomUUID(),
      code: course.code,
      name: course.name,
      section: section.section,
      credits: meetingIndex === 0 ? course.credits : 0,
      color: courseIndex % COLORS.length,
      kind: "course" as const,
      instructor: section.instructor,
      ...meeting,
    })));
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

function readSavedPlan(): Partial<SavedPlan> | null {
  // The old explicit Save button wrote to STORAGE_KEY. Autosave writes to
  // PLAN_KEY, so without this fallback the first load after the change looks
  // to a returning student exactly like their schedule was deleted.
  const raw = window.localStorage.getItem(PLAN_KEY) ?? window.localStorage.getItem(STORAGE_KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as unknown;
    return parsed && typeof parsed === "object" ? (parsed as Partial<SavedPlan>) : null;
  } catch {
    return null;
  }
}

export function SchedulePlanner() {
  const { pick } = useLocale();
  const t = useCallback((tr: string, en: string) => pick({ tr, en }), [pick]);

  const [term] = useState(() => upcomingTerm());
  const [entries, setEntries] = useState<Entry[]>([]);
  const [favorites, setFavorites] = useState<Entry[][]>([]);
  const [favoriteIndex, setFavoriteIndex] = useState(-1);
  const [department, setDepartment] = useState("");
  const [departmentLabel, setDepartmentLabel] = useState("");
  const [departmentBusy, setDepartmentBusy] = useState(true);
  const [departmentQuery, setDepartmentQuery] = useState("");
  const [departmentOptions, setDepartmentOptions] = useState<DepartmentOption[]>([]);
  const [departmentSearching, setDepartmentSearching] = useState(false);
  const [departmentStatus, setDepartmentStatus] = useState<"ok" | "unknown" | "failed" | "disconnected">("ok");
  const [departmentCodeDraft, setDepartmentCodeDraft] = useState("");
  const departmentKnown = Boolean(department.trim());
  // Read from the student context (SAIS), never typed here: it is academic
  // data that belongs in Settings, and a stale copy in one browser silently
  // overwriting the registrar's value is not a trade worth one input box.
  const [surname, setSurname] = useState("");
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
  const [poolQuery, setPoolQuery] = useState("");
  const [suggestions, setSuggestions] = useState<CatalogCourse[]>([]);
  const [suggestBusy, setSuggestBusy] = useState(false);
  const [suggestNote, setSuggestNote] = useState("");
  const [manualOpen, setManualOpen] = useState(false);
  const [manualBusy, setManualBusy] = useState(false);
  const [draft, setDraft] = useState({ code: "", name: "", section: "1", day: "Mon" as Day, start: 9, duration: 1, room: "", credits: 3 });
  // Nothing may be written back to storage until the stored plan has been
  // read, or the empty first render erases the plan it is about to load.
  const [hydrated, setHydrated] = useState(false);
  const busy = planBusy || generateProgress !== null;

  // --- persistence --------------------------------------------------------

  // Deferred by a timer, like every other localStorage read in this app: the
  // stored plan is not part of the server-rendered markup, so restoring it
  // during the effect body would be a synchronous cascading render over
  // markup React has only just committed.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const shared = window.location.hash.startsWith("#plan=") ? window.location.hash.slice(6) : "";
      const plan = readSavedPlan();
      if (plan) {
        // Every field is checked rather than trusted: this is a value the
        // user's own browser holds, so an older or hand-edited shape must not
        // be able to push `undefined` into a controlled input.
        if (Array.isArray(plan.entries)) setEntries(plan.entries);
        if (Array.isArray(plan.pool)) setCatalogCourses(plan.pool);
        if (plan.sections && typeof plan.sections === "object") setSectionsByCourse(plan.sections);
        if (typeof plan.department === "string") setDepartment(plan.department);
        if (typeof plan.departmentLabel === "string") setDepartmentLabel(plan.departmentLabel);
        if (Array.isArray(plan.emptyDays)) setEmptyDays(plan.emptyDays.filter((day): day is Day => DAYS.includes(day as Day)));
        // Plans saved when this was a single day still carry `emptyDay`.
        else if (typeof (plan as { emptyDay?: string }).emptyDay === "string" && DAYS.includes((plan as { emptyDay?: string }).emptyDay as Day)) {
          setEmptyDays([(plan as { emptyDay?: string }).emptyDay as Day]);
        }
        if (typeof plan.avoidConflicts === "boolean") setAvoidConflicts(plan.avoidConflicts);
        if (typeof plan.ignoreConstraints === "boolean") setIgnoreConstraints(plan.ignoreConstraints);
        // Kept so a reload does not silently collapse a set of alternatives
        // back to whichever one happened to be on screen.
        if (Array.isArray(plan.alternatives)) setAlternatives(plan.alternatives.filter(Array.isArray));
        if (typeof plan.alternativeIndex === "number") setAlternativeIndex(plan.alternativeIndex);
      }
      // A shared link is an explicit instruction and outranks the stored plan.
      if (shared) {
        try { setEntries(JSON.parse(decodeURIComponent(escape(atob(shared)))) as Entry[]); } catch { /* malformed shared plan */ }
      }
      const raw = window.localStorage.getItem(`${STORAGE_KEY}:favorites`);
      if (raw) try { setFavorites(JSON.parse(raw) as Entry[][]); } catch { /* ignore corrupt local draft */ }
      setHydrated(true);
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    const payload: SavedPlan = { entries, department, departmentLabel, emptyDays, avoidConflicts, ignoreConstraints, pool: catalogCourses, sections: sectionsByCourse, alternatives, alternativeIndex };
    try { window.localStorage.setItem(PLAN_KEY, JSON.stringify(payload)); } catch { /* quota, or storage blocked */ }
  }, [hydrated, entries, department, departmentLabel, emptyDays, avoidConflicts, ignoreConstraints, catalogCourses, sectionsByCourse, alternatives, alternativeIndex]);

  // The timetable also goes to the broker, so chat can answer questions about
  // the week being built here. Only this projection — the courses, their
  // sections and when they meet — not the pool, the cached section lists or
  // the alternatives, which are working state and would be injected into
  // every chat turn for nothing.
  //
  // Debounced well past the drag of a single course: the grid changes on every
  // click, and each change is not worth a request.
  useEffect(() => {
    if (!hydrated) return;
    const timer = window.setTimeout(() => {
      const courses = new Map<string, { code: string; name: string; section: string; credits: number; instructor: string; meetings: { day: Day; start: number; duration: number; room: string }[] }>();
      const blocks = new Map<string, { name: string; meetings: { day: Day; start: number; duration: number; room: string }[] }>();
      for (const entry of entries) {
        const meeting = { day: entry.day, start: entry.start, duration: entry.duration, room: entry.room ?? "" };
        if (entry.kind === "block") {
          const key = entry.name || entry.id;
          (blocks.get(key) ?? blocks.set(key, { name: entry.name, meetings: [] }).get(key)!).meetings.push(meeting);
          continue;
        }
        // One row per course-and-section, carrying every hour it meets.
        const key = `${entry.code}|${entry.section}`;
        const existing = courses.get(key)
          ?? courses.set(key, { code: entry.code, name: entry.name, section: entry.section, credits: 0, instructor: entry.instructor ?? "", meetings: [] }).get(key)!;
        existing.credits += entry.credits;
        existing.meetings.push(meeting);
      }
      void jsonFetch("/api/schedule/timetable", {
        method: "PUT",
        body: { term, courses: [...courses.values()].slice(0, 20), busy_blocks: [...blocks.values()].slice(0, 20) },
      }).catch(() => { /* the browser copy is the one that matters; chat catches up next change */ });
    }, 2500);
    return () => window.clearTimeout(timer);
  }, [hydrated, entries, term]);

  useEffect(() => {
    let cancelled = false;
    void jsonFetch<{ department_query: string | null; department_code: string | null; surname_prefix: string | null }>("/api/schedule/student-context")
      .then((context) => {
        if (cancelled) return;
        // Section restrictions are written as surname ranges, so this is what
        // makes them apply at all. Only ever fills an empty box: a student who
        // typed something themselves is not overwritten.
        if (context.surname_prefix) {
          setSurname((current) => current.trim() || context.surname_prefix!);
        }
        // Resolved against the catalog server-side. Scanning the payload here
        // for any three-digit run used to pick up a year or a row count and
        // then quietly load another department's courses.
        // Some SAIS records expose an official abbreviation (for example EE)
        // while the catalog lookup cannot resolve a numeric department code.
        // The AI planner accepts that verified SAIS value, so do not leave the
        // actionable state empty while displaying a valid department label.
        const resolved = context.department_code ?? context.department_query ?? "";
        // Only overwrite a restored manual choice when SAIS actually knows: a
        // student who picked their department by hand keeps that pick.
        if (resolved) {
          setDepartment(resolved);
          setDepartmentLabel(context.department_query ?? resolved);
        } else {
          setDepartmentStatus("unknown");
        }
      })
      .catch((error) => {
        // Distinguished from "SAIS answered and had nothing": the picker is the
        // way out of both, but a student whose campus connection has expired
        // needs to be told that rather than left guessing why their department
        // vanished.
        if (!cancelled) setDepartmentStatus(error instanceof Error && /401|403|connect/i.test(error.message) ? "disconnected" : "failed");
      })
      .finally(() => { if (!cancelled) setDepartmentBusy(false); });
    // The student context is stable for the lifetime of this page.
    return () => { cancelled = true; };
  }, []);

  // --- department picker --------------------------------------------------

  // What the server already holds, so an unchanged value is not re-sent.
  const searchToken = useRef(0);
  useEffect(() => {
    const query = departmentQuery.trim();
    // A token rather than an abort: a slow first response must not be allowed
    // to land after a later one and replace the list the student is reading.
    // Every state change happens inside the debounce, so a keystroke costs no
    // synchronous render of its own.
    const token = ++searchToken.current;
    const timer = window.setTimeout(() => {
      if (departmentKnown || query.length < 2) {
        setDepartmentOptions([]);
        setDepartmentSearching(false);
        return;
      }
      setDepartmentSearching(true);
      void jsonFetch<{ departments?: DepartmentOption[] }>(`/api/schedule/departments/search?query=${encodeURIComponent(query)}`)
        .then((response) => { if (token === searchToken.current) setDepartmentOptions(response.departments ?? []); })
        .catch((error) => {
        // The planner is driven imperatively rather than through TanStack
        // Query, so none of its failures reached the central query reporter.
        captureRequestFailure(error, { operation: "schedule.student_context", kind: "query" }); if (token === searchToken.current) setDepartmentOptions([]); })
        .finally(() => { if (token === searchToken.current) setDepartmentSearching(false); });
    }, 350);
    return () => window.clearTimeout(timer);
  }, [departmentQuery, departmentKnown]);

  function chooseDepartment(option: DepartmentOption) {
    setDepartment(option.code);
    setDepartmentLabel(option.name || option.code);
    setDepartmentQuery("");
    setDepartmentOptions([]);
    setDepartmentCodeDraft("");
    setDepartmentStatus("ok");
  }

  function applyDepartmentCode() {
    const code = departmentCodeDraft.replace(/\D/g, "");
    if (code.length !== 3) return toast.error(t("Bölüm kodu üç haneli olmalı.", "A department code is three digits."));
    chooseDepartment({ code, name: code });
  }

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

  const dayLabel = useCallback((day: Day) => ({ Mon: t("Pzt", "Mon"), Tue: t("Sal", "Tue"), Wed: t("Çar", "Wed"), Thu: t("Per", "Thu"), Fri: t("Cum", "Fri") })[day], [t]);
  // What distinguishes one alternative from the next, in the terms a student
  // is choosing on: which days it leaves free.
  const currentShape = useMemo(() => (entries.length ? scheduleShape(entries) : null), [entries]);
  // Recomputed only when the timetable changes, not per cell: the grid has
  // fifty cells and this is a whole-week assignment.
  const gridLanes = useMemo(() => laneLayout(entries), [entries]);
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
          return toast.error(verdict.reason
            ? t(`Şube ${section} sana kapalı: ${verdict.reason}. Yine de eklemek için "Kısıtları yok say"ı aç.`,
                `Section ${section} is closed to you: ${verdict.reason}. Turn on "ignore restrictions" to add it anyway.`)
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
    const rawCode = (chosen?.rawCode ?? poolQuery).toUpperCase().replace(/[^A-Z0-9]/g, "");
    if (!rawCode) return toast.error(t("Ders kodu gerekli.", "Course code is required."));
    if (catalogCourses.some((course) => courseIdentity(course.rawCode) === courseIdentity(rawCode))) return toast.error(t("Bu ders zaten listede.", "This course is already in the list."));
    const course: CatalogCourse = chosen ?? { rawCode, code: rawCode, name: rawCode, credits: 0 };
    setCatalogCourses((current) => [...current, course]);
    setPoolQuery("");
    setSuggestions([]);
    void fetchAllConstraints([course]);
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
        setSuggestions(response.courses.map((item) => ({ rawCode: item.code, code: item.code, name: item.name, credits: item.credits })));
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
    const response = await jsonFetch<{ data: unknown }>(`/api/schedule/courses/${encodeURIComponent(course.rawCode)}?department=${encodeURIComponent(courseDepartment)}&semester=${encodeURIComponent(term)}`);
    const sections = parseSections(response.data);
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
    const response = await jsonFetch<{ courses?: Record<string, { data?: unknown; error?: string }> }>(
      "/api/schedule/sections",
      { method: "POST", body: { semester: term, department: department.trim() || undefined, courses: wanted.map((course) => course.rawCode) } },
    );
    const found: SectionMap = {};
    for (const [rawCode, payload] of Object.entries(response.courses ?? {})) {
      if (payload?.error !== undefined) continue;
      found[courseIdentity(rawCode)] = parseSections(payload?.data);
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
          const next: Record<string, Record<string, SectionVerdict>> = {};
          for (const [rawCode, payload] of Object.entries(response.courses ?? {})) {
            next[courseIdentity(rawCode)] = payload?.sections ?? {};
          }
          setConstraints((current) => ({ ...current, ...next }));
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

  /** Whether the student may register, preferring METU's own table. */
  const sectionAllowed = useCallback((course: CatalogCourse, section: CatalogSection) => {
    const verdict = constraints[courseIdentity(course.rawCode)]?.[section.section];
    if (verdict) return { allowed: verdict.eligible, reason: verdict.reason };
    // No table yet: fall back to the free-text note, which is usually empty.
    return { allowed: surnameEligible(section, surname), reason: "" };
  }, [constraints, surname]);

  async function requestCurriculum(courses: CatalogCourse[]) {
    // This was the slowest thing a student waited on in the whole app — a
    // median of 95.8 seconds — because it ran an agent that called one catalog
    // tool per department with a model turn between each. It now reads the
    // curriculum directly and answers in about two.
    const startedAt = Date.now();
    let response: { courses?: AiPlanCourse[]; warnings?: string[]; cache_hit?: boolean; duration_ms?: number };
    try {
      response = await jsonFetch<{ courses?: AiPlanCourse[]; warnings?: string[]; cache_hit?: boolean; duration_ms?: number }>("/api/schedule/curriculum", {
        method: "POST",
        // Omitted rather than sent empty: the broker reads the department from
        // the stored campus context when the client has none, so a page whose
        // own state is empty still gets a plan instead of a validation error.
        body: { department: department.trim() || undefined, semester: term, courses: courses.map((course) => ({ code: course.rawCode })) },
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
      sectionMap[courseIdentity(rawCode)] = parseSections(item.sections ?? []);
      return [{ rawCode, code: rawCode.toUpperCase(), name: String(item.name ?? rawCode), credits: Number(item.credits ?? 0) }];
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
    return { courses: verified, warnings, cacheHit: response.cache_hit, durationMs: response.duration_ms };
  }

  async function loadRequiredCourses() {
    // No department gate any more. The broker resolves it from the stored
    // campus context when the request omits it, and answers with a specific
    // 422 when it genuinely has none — which is more than this check knew.
    setPlanBusy(true); setCurriculumNotice(""); setExpandedCourse(null);
    try {
      const result = await requestCurriculum([]);
      setCatalogCourses(result.courses);
      // Every course the curriculum names, checked now rather than when the
      // student happens to expand one: the red flags have to be visible while
      // they are choosing, not after.
      void fetchAllConstraints(result.courses);
      const timing = typeof result.durationMs === "number" ? ` (${(result.durationMs / 1000).toFixed(1)} sn)` : "";
      const cacheLabel = result.cacheHit ? t(" · kalıcı önbellekten", " · from persistent cache") : "";
      setCurriculumNotice(result.courses.length
        ? t(`Müfredatından bu dönem açılan ${result.courses.length} ders bulundu${timing}${cacheLabel}.${result.warnings.length ? ` ${result.warnings.join(" ")}` : ""}`, `${result.courses.length} courses from your curriculum are offered this term${timing}${cacheLabel}.${result.warnings.length ? ` ${result.warnings.join(" ")}` : ""}`)
        : t(`Müfredatında bu dönem açılan, henüz almadığın bir ders bulunamadı.${result.warnings.length ? ` ${result.warnings.join(" ")}` : ""}`, `No course from your curriculum that you still need is offered this term.${result.warnings.length ? ` ${result.warnings.join(" ")}` : ""}`));
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
    setGenerateProgress({ done: 0, total: catalogCourses.length });
    try {
      const unavailable: string[] = [];
      const unpublished: string[] = [];
      const blocked: string[] = [];
      const restricted: string[] = [];
      const placeable: CourseOptions[] = [];
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
        // The empty day is a hard filter applied before the search, not a
        // scoring preference: it holds whether or not conflict prevention is
        // on, which is what addEntry already does.
        const withinDay = timed.filter((section) => section.meetings.every((meeting) => !emptyDays.includes(meeting.day)));
        // Section restrictions are real at METU — a section reserved for
        // surnames A-K is not one this student can register for — so they
        // filter the search rather than merely annotating the result. The
        // override exists because the notes are free text and this only
        // understands one of the things they can say.
        const allowed = ignoreConstraints ? withinDay : withinDay.filter((section) => sectionAllowed(course, section).allowed);
        if (!allowed.length) {
          if (withinDay.length && !ignoreConstraints) restricted.push(course.code);
          else blocked.push(course.code);
          continue;
        }
        placeable.push({ course, options: allowed });
      }

      const solutions = buildAlternatives(placeable, avoidConflicts);
      let ranked: Entry[][];
      let unplaced = [...blocked];

      if (solutions.length) {
        ranked = solutions
          .map((choices) => ({ entries: choiceEntries(choices), shape: scheduleShape(choiceEntries(choices)) }))
          .sort((a, b) => a.shape.dayCount - b.shape.dayCount || a.shape.gaps - b.shape.gaps)
          .map((item) => item.entries);
      } else {
        // No arrangement takes every course. Rather than showing nothing, fall
        // back to filling what fits and naming what had to be dropped.
        const { chosen, skipped } = greedyPlan(placeable, avoidConflicts);
        ranked = chosen.length ? [choiceEntries(chosen)] : [];
        unplaced = [...unplaced, ...skipped];
      }

      setAlternatives(ranked);
      setAlternativeIndex(0);
      setEntries(ranked[0] ?? []);

      if (unavailable.length) toast.error(t(`${unavailable.join(", ")} için ODTÜ sisteminde şube bulunamadı.`, `No sections were found in METU's system for ${unavailable.join(", ")}.`));
      if (unpublished.length) toast.warning(t(`${unpublished.join(", ")} için gün ve saat ODTÜ tarafından henüz yayımlanmadı.`, `METU has not published days and times for ${unpublished.join(", ")} yet.`));
      if (restricted.length) toast.warning(t(`${restricted.join(", ")} için soyadına açık şube yok. Kısıtları yok sayarak tekrar dene.`, `No section of ${restricted.join(", ")} is open to your surname. Try again with restrictions ignored.`));
      if (unplaced.length) toast.warning(t(`${unplaced.join(", ")} için boş gün ve çakışma tercihlerine uyan şube yok.`, `No section of ${unplaced.join(", ")} fits your empty-day and conflict preferences.`));
      if (ranked.length > 1) {
        const capped = ranked.length >= MAX_ALTERNATIVES;
        toast.success(t(
          `${capped ? "En az " : ""}${ranked.length} alternatif program bulundu. Oklarla aralarında geçiş yap.`,
          `${capped ? "At least " : ""}${ranked.length} possible schedules found. Use the arrows to switch between them.`,
        ));
      } else if (ranked.length === 1 && !unavailable.length && !unpublished.length && !unplaced.length) {
        toast.success(t("Tek bir çakışmasız program mümkün.", "Exactly one conflict-free schedule is possible."));
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("Program oluşturulamadı.", "The schedule could not be generated."));
    } finally { setGenerateProgress(null); }
  }

  function showAlternative(index: number) {
    if (alternatives.length < 2) return;
    const next = (index + alternatives.length) % alternatives.length;
    setAlternativeIndex(next);
    setEntries(alternatives[next]);
  }

  function addCatalogSection(course: CatalogCourse, section: CatalogSection) {
    if (!section.meetings.length) return toast.warning(t("Bu şubenin gün ve saati ODTÜ tarafından henüz yayımlanmamış.", "METU has not published this section's day and time yet."));
    // Refused, not merely flagged. A section the student cannot register for
    // does not belong on the timetable they are building their week around,
    // and the verdict now comes from METU's own eligibility table rather than
    // from reading a free-text note. The override stays because the table can
    // be missing and because the student is the one who knows their own case.
    const check = sectionAllowed(course, section);
    if (!check.allowed && !ignoreConstraints) {
      return toast.error(
        check.reason
          ? t(`Şube ${section.section} sana kapalı: ${check.reason}. Eklemek için "Kısıtları yok say"ı aç.`,
              `Section ${section.section} is closed to you: ${check.reason}. Turn on "ignore restrictions" to add it anyway.`)
          : t(`Şube ${section.section} kısıtlarına uymuyorsun. Eklemek için "Kısıtları yok say"ı aç.`,
              `You do not meet section ${section.section}'s restrictions. Turn on "ignore restrictions" to add it anyway.`),
      );
    }
    if (!check.allowed) {
      toast.warning(t(`Şube ${section.section} kısıtlara uymuyor, kısıtlar yok sayıldığı için eklendi.`,
        `Section ${section.section} does not meet its restrictions; added because restrictions are ignored.`));
    }
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
    try { window.localStorage.setItem(`${STORAGE_KEY}:favorites`, JSON.stringify(next)); } catch { /* quota, or storage blocked */ }
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
    const summary = DAYS.map((day) => `${dayLabel(day)}: ${entries.filter((e) => e.day === day).sort((a, b) => a.start - b.start).map((e) => `${String(e.start).padStart(2, "0")}:40 ${e.code}-${e.section} (${e.kind === "course" ? (e.room?.trim() || "TBA") : (e.room?.trim() || "—")})`).join(", ") || "—"}`).join("\n");
    await navigator.clipboard.writeText(summary); toast.success(t("Program özeti kopyalandı.", "Schedule summary copied."));
  }

  async function copyShareLink() {
    const encoded = btoa(unescape(encodeURIComponent(JSON.stringify(entries))));
    await navigator.clipboard.writeText(`${window.location.origin}/schedule#plan=${encoded}`);
    toast.success(t("Salt okunur paylaşım bağlantısı kopyalandı.", "Read-only share link copied."));
  }

  function exportCsv() {
    if (!entries.length) return toast.error(t("Dışa aktarılacak ders yok.", "There is nothing to export yet."));
    const header = [t("Gün", "Day"), t("Başlangıç", "Start"), t("Bitiş", "End"), t("Kod", "Code"), t("Ders adı", "Course name"), t("Şube", "Section"), t("Öğretim elemanı", "Instructor"), t("Derslik", "Room"), t("Kredi", "Credits")];
    const rows = [...entries]
      .sort((a, b) => DAYS.indexOf(a.day) - DAYS.indexOf(b.day) || a.start - b.start)
      .map((entry) => [dayLabel(entry.day), `${String(entry.start).padStart(2, "0")}:40`, `${String(entry.start + entry.duration).padStart(2, "0")}:40`, entry.code, entry.name, entry.section, entry.instructor ?? "", entry.kind === "course" ? (entry.room?.trim() || "TBA") : entry.room, entry.credits]);
    const csv = [header, ...rows].map((row) => row.map(csvCell).join(";")).join("\r\n");
    downloadFile(`devrimo-${term}.csv`, new Blob([`﻿${csv}`], { type: "text/csv;charset=utf-8" }));
    toast.success(t("Program CSV olarak indirildi.", "Schedule downloaded as CSV."));
  }

  function exportWallpaper() {
    const width = 3840, height = 2160;
    const cells = entries.map((e) => `<rect x="${460 + DAYS.indexOf(e.day) * 650}" y="${330 + (e.start - 8) * 170}" width="610" height="${e.duration * 160}" rx="24" fill="#e31837" opacity=".9"/><text x="${490 + DAYS.indexOf(e.day) * 650}" y="${390 + (e.start - 8) * 170}" fill="white" font-size="36" font-family="Arial" font-weight="700">${e.code.replace(/[<>&]/g, "")}</text>`).join("");
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><rect width="100%" height="100%" fill="#171312"/><text x="180" y="170" fill="white" font-size="72" font-family="Arial" font-weight="700">Devrimo · ${termLabel(term, t).replace(/[<>&]/g, "")}</text>${DAYS.map((d, i) => `<text x="${500 + i * 650}" y="285" fill="#aaa" font-size="36" font-family="Arial">${dayLabel(d)}</text>`).join("")}${cells}</svg>`;
    downloadFile("devrimo-schedule-4k.svg", new Blob([svg], { type: "image/svg+xml" }));
  }

  // --- render -------------------------------------------------------------
  // Below xl the page scrolls as one column. From xl up it is pinned to the
  // viewport: the sidebar and the timetable each scroll inside themselves, so
  // the week is always fully visible without moving the page.

  return (
    <div className="h-full overflow-y-auto bg-[radial-gradient(circle_at_85%_0%,rgb(227_24_55/8%),transparent_32%)] px-4 py-4 sm:px-6 lg:px-8 xl:flex xl:flex-col xl:overflow-hidden">
      <div className="mx-auto w-full max-w-[1500px] space-y-4 xl:flex xl:min-h-0 xl:flex-1 xl:flex-col xl:gap-4 xl:space-y-0">
        <div className="flex flex-wrap items-center justify-between gap-3 xl:shrink-0">
          <div className="flex min-w-0 flex-wrap items-baseline gap-x-3">
            <h1 className="text-2xl font-semibold tracking-tight">{t("Ders programı", "Schedule")}</h1>
            <p className="truncate text-sm text-muted-foreground">{termLabel(term, t)} · {t("derslerini ekle, çakışmaları gör, paylaş", "add courses, spot conflicts, share")}</p>
          </div>
          <Button variant="outline" size="sm" onClick={() => setEntries([])}><RotateCcwIcon />{t("Programı temizle", "Clear schedule")}</Button>
        </div>

        {/* Above both columns: it explains the whole screen, and once dismissed
            it shrinks to a single link rather than taking space forever. */}
        <PlannerIntro className="mb-4" />

        <div className="grid gap-4 xl:min-h-0 xl:flex-1 xl:grid-cols-[340px_minmax(0,1fr)]">
          <aside className="min-w-0 space-y-4 xl:min-h-0 xl:overflow-y-auto xl:pr-1">
            <Card><CardContent className="grid grid-cols-[minmax(0,1fr)] gap-3 p-4">
              {/* The department is not a setting. It is read once from SAIS and
                  the broker keeps it, so in the normal case there is nothing
                  here to show or decide — this whole block appears only when
                  the campus systems gave us nothing to go on. */}
              {departmentBusy || departmentKnown ? null : (
                <Field label={t("Bölüm", "Department")}>
                  <div className="space-y-2">
                    {!departmentKnown ? (
                      <p className="text-xs text-muted-foreground">
                        {departmentStatus === "disconnected"
                          ? t("ODTÜ bağlantın yenilenmeli; bölümün SAIS'ten okunamadı. Aşağıdan seçebilirsin.", "Your METU connection needs renewing, so your department could not be read from SAIS. Pick it below.")
                          : departmentStatus === "failed"
                            ? t("SAIS'e ulaşılamadı. Bölümünü aşağıdan seç.", "SAIS could not be reached. Pick your department below.")
                            : t("SAIS bir bölüm bildirmedi. Aşağıdan seç.", "SAIS did not report a department. Pick one below.")}
                      </p>
                    ) : null}
                    <div className="relative">
                      <SearchIcon className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                      <Input value={departmentQuery} onChange={(e) => setDepartmentQuery(e.target.value)} className="pl-9" placeholder={t("Bölüm ara (ör. Bilgisayar)", "Search department (e.g. Computer)")} aria-label={t("Bölüm ara", "Search department")} />
                    </div>
                    {departmentSearching ? <p className="flex items-center gap-2 text-xs text-muted-foreground"><Loader2Icon className="size-3.5 animate-spin" />{t("Aranıyor…", "Searching…")}</p> : null}
                    {departmentOptions.length ? (
                      <select
                        value=""
                        onChange={(e) => { const found = departmentOptions.find((option) => option.code === e.target.value); if (found) chooseDepartment(found); }}
                        className="h-10 w-full rounded-md border bg-background px-2 text-sm"
                        aria-label={t("Bölüm seç", "Select department")}
                      >
                        <option value="" disabled>{t(`${departmentOptions.length} bölüm bulundu — seç`, `${departmentOptions.length} departments found — select`)}</option>
                        {departmentOptions.map((option) => <option key={option.code} value={option.code}>{option.name} ({option.code})</option>)}
                      </select>
                    ) : !departmentSearching && departmentQuery.trim().length >= 2 ? (
                      <p className="text-xs text-muted-foreground">{t("Eşleşen bölüm bulunamadı.", "No matching department was found.")}</p>
                    ) : null}
                    {/* Last resort, and the only branch that needs no campus
                        server at all: the catalog search goes through the
                        student's own Course Info connection, so when that is
                        what is broken the search cannot be the only way in. */}
                    <div className="flex items-center gap-2">
                      <Input
                        value={departmentCodeDraft}
                        onChange={(e) => setDepartmentCodeDraft(e.target.value.replace(/\D/g, "").slice(0, 3))}
                        onKeyDown={(e) => { if (e.key === "Enter") applyDepartmentCode(); }}
                        inputMode="numeric"
                        className="h-9"
                        placeholder={t("veya üç haneli kod (567)", "or three-digit code (567)")}
                        aria-label={t("Bölüm kodunu elle gir", "Enter department code manually")}
                      />
                      <Button size="sm" variant="outline" className="shrink-0" onClick={applyDepartmentCode} disabled={departmentCodeDraft.length !== 3}>{t("Kullan", "Use")}</Button>
                    </div>
                  </div>
                </Field>
              )}
              <Field label={t("Boş günler", "Empty days")}>
                {/* Five fixed options, so toggles rather than a multi-select:
                    every choice is visible and one click wide, and a dropdown
                    would hide the current selection behind a summary line. */}
                <div className="flex gap-1">
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
              {constraintsBusy ? <p className="flex items-center gap-2 rounded-lg border bg-muted/30 p-2 text-xs leading-5 text-muted-foreground"><Loader2Icon className="size-3.5 shrink-0 animate-spin" />{t("Şube kısıtları ODTÜ'den okunuyor; kırmızı işaretler geldikçe belirecek.", "Reading section restrictions from METU; red flags will appear as they arrive.")}</p> : null}
              <div className="space-y-2" data-tour="search">
                <div className="relative">
                  <SearchIcon className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    value={poolQuery}
                    onChange={(e) => setPoolQuery(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") addPoolCourse(); }}
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
                        <span className="min-w-0 flex-1 break-words text-xs leading-snug text-muted-foreground">{item.name}</span>
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
                            <span className="min-w-0 flex-1"><span className="block font-semibold">{course.code}</span><span className="block truncate text-xs text-muted-foreground">{course.name}</span></span>
                            <ChevronDownIcon className={cn("size-4 shrink-0 transition-transform", expanded && "rotate-180")} />
                          </button>
                          <Button size="icon" variant="ghost" aria-label={t(`${course.code} dersini havuzdan çıkar`, `Remove ${course.code} from course pool`)} onClick={() => removePoolCourse(course)}><Trash2Icon /></Button>
                        </div>
                        {expanded ? <div className="space-y-2 border-t p-2">
                          {sectionsBusy === identity ? <p className="flex items-center gap-2 text-xs text-muted-foreground"><Loader2Icon className="size-3.5 animate-spin" />{t("Şubeler getiriliyor…", "Loading sections…")}</p>
                            : sections.length ? sections.map((section) => {
                              const check = sectionAllowed(course, section);
                              const eligible = check.allowed;
                              const closedLabel = check.reason
                                ? t(`Bu şube sana kapalı: ${check.reason}.`, `This section is closed to you: ${check.reason}.`)
                                : t("Bu şubenin kısıtlarına uymuyorsun.", "You do not meet this section's restrictions.");
                              return (
                                <button
                                  key={section.section}
                                  onClick={() => addCatalogSection(course, section)}
                                  className={cn("w-full rounded-lg border bg-background p-2 text-left text-sm transition hover:border-primary/40 hover:bg-primary/5", !eligible && !ignoreConstraints && "opacity-60")}
                                >
                                  <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                                    <span className="font-semibold">{t("Şube", "Section")} {section.section}</span>
                                    {!eligible ? <Tooltip>
                                      {/* No tabIndex: this sits inside the
                                          section button, and a focusable
                                          inside a button is not reachable in
                                          the way it looks like it should be.
                                          The reason is in the sr-only text
                                          below, which the button announces. */}
                                      <TooltipTrigger render={<span className="inline-flex shrink-0 text-destructive" />}>
                                        <TriangleAlertIcon className="size-3.5" aria-hidden="true" />
                                        <span className="sr-only">{closedLabel}</span>
                                      </TooltipTrigger>
                                      <TooltipContent className="max-w-56 text-xs">{closedLabel}</TooltipContent>
                                    </Tooltip> : null}
                                  </span>
                                  {section.instructor ? <span className="mt-0.5 block break-words text-xs font-medium text-foreground/80">{section.instructor}</span> : null}
                                  <span className="mt-1 block break-words text-xs text-muted-foreground">{section.meetings.length ? section.meetings.map((meeting) => `${dayLabel(meeting.day)} ${String(meeting.start).padStart(2, "0")}:40 · ${meeting.room?.trim() || "TBA"}`).join(" / ") : t("Gün ve saat henüz yayımlanmadı", "Day and time not published yet")}</span>
                                  {/* Shown verbatim. It is a free-text box a
                                      department types into, so only the surname
                                      range is ever interpreted — everything else
                                      the student has to read for themselves. */}
                                  {section.constraint ? <span className="mt-1 block rounded bg-muted/50 px-1.5 py-1 text-[11px] leading-snug text-muted-foreground">{section.constraint}</span> : null}
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
                  <div className="min-w-0"><p className="truncate text-sm font-medium">{entry.code} · {entry.name}</p><p className="text-xs text-muted-foreground">{courseEntries.map((meeting) => `${dayLabel(meeting.day)} ${String(meeting.start).padStart(2, "0")}:40`).join(" / ")} · {t("Şube", "Section")} {entry.section}{entry.instructor ? ` · ${entry.instructor}` : ""}</p></div>
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
                <div className="grid grid-cols-2 gap-2"><Field label={t("Kod", "Code")}><Input value={draft.code} onChange={(e) => setDraft({ ...draft, code: e.target.value })} placeholder="MATH 260" /></Field><Field label={t("Şube", "Section")}><Input value={draft.section} onChange={(e) => setDraft({ ...draft, section: e.target.value })} /></Field></div>
                <Field label={t("Ders adı", "Course name")}><Input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder={t("Temel Lineer Cebir", "Basic Linear Algebra")} /></Field>
                <div className="grid grid-cols-3 gap-2"><Field label={t("Gün", "Day")}><select value={draft.day} onChange={(e) => setDraft({ ...draft, day: e.target.value as Day })} className="h-10 w-full rounded-md border bg-background px-2 text-sm">{DAYS.map((d) => <option key={d} value={d}>{dayLabel(d)}</option>)}</select></Field><Field label={t("Başlangıç", "Start")}><select value={draft.start} onChange={(e) => setDraft({ ...draft, start: Number(e.target.value) })} className="h-10 w-full rounded-md border bg-background px-2 text-sm">{HOURS.map((h) => <option key={h} value={h}>{String(h).padStart(2, "0")}:40</option>)}</select></Field><Field label={t("Süre", "Hours")}><select value={draft.duration} onChange={(e) => setDraft({ ...draft, duration: Number(e.target.value) })} className="h-10 w-full rounded-md border bg-background px-2 text-sm">{[1, 2, 3].map((h) => <option key={h}>{h}</option>)}</select></Field></div>
                <div className="grid grid-cols-2 gap-2"><Field label={t("Derslik", "Room")}><Input value={draft.room} onChange={(e) => setDraft({ ...draft, room: e.target.value })} placeholder="M-13" /></Field><Field label={t("Kredi", "Credits")}><Input type="number" min={0} max={10} value={draft.credits} onChange={(e) => setDraft({ ...draft, credits: Number(e.target.value) })} /></Field></div>
                <Button onClick={() => void addEntry()} disabled={manualBusy}>{manualBusy ? <Loader2Icon className="animate-spin" /> : <PlusIcon />}{t("Programa ekle", "Add to schedule")}</Button>
              </CardContent> : null}
            </Card>
          </aside>

          {/* min-w-0 is load-bearing: the timetable carries a min-width so its
              five day columns stay legible, and without this the single grid
              column below xl grows to that width and takes the whole page
              sideways with it. */}
          <main className="min-w-0 space-y-3 xl:flex xl:min-h-0 xl:flex-col xl:gap-3 xl:space-y-0">
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
              <CardContent className="overflow-auto p-0 xl:min-h-0 xl:flex-1" data-tour="grid">
                <div
                  className="grid min-w-[820px] grid-cols-[68px_repeat(5,minmax(130px,1fr))] border-t text-sm xl:h-full"
                  style={{ gridTemplateRows: `auto repeat(${HOURS.length}, minmax(${ROW_MIN_PX}px, 1fr))` }}
                >
                  {/* No bottom border: the 08:40 label sits on this line, and a
                      rule through the text is the thing being removed. Sticky
                      with the day names, or the corner slides under them. */}
                  <div className="sticky top-0 z-30 border-r bg-card" />
                  {DAYS.map((d) => <div key={d} className={cn("sticky top-0 z-30 border-b border-r bg-card px-2 py-2 text-center text-sm font-semibold", emptyDays.includes(d) && "bg-primary/10 text-primary")}>{dayLabel(d)}</div>)}
                  {HOURS.flatMap((hour) => [
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
                      <span className={cn("absolute right-2 text-[11px] font-medium leading-none tabular-nums text-muted-foreground", hour === HOURS[0] ? "top-1.5" : "top-0 -translate-y-1/2")}>
                        {String(hour).padStart(2, "0")}:40
                      </span>
                      {hour === HOURS[HOURS.length - 1] ? (
                        <span className="absolute bottom-0 right-2 translate-y-1/2 text-[11px] font-medium leading-none tabular-nums text-muted-foreground">
                          {String(hour + 1).padStart(2, "0")}:40
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
                              <span className="mt-0.5 block truncate text-xs opacity-85">{entry.name}</span>
                            </button>
                            );
                          })}
                        </div>
                      );
                    }),
                  ])}
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
              <Button variant="outline" onClick={() => void copyShareLink()}><ClipboardIcon />{t("Paylaşım bağlantısı", "Share link")}</Button>
              <Button variant="outline" onClick={exportCsv}><DownloadIcon />{t("CSV olarak indir", "Download as CSV")}</Button>
              <Button variant="outline" onClick={exportWallpaper}><DownloadIcon />{t("4K duvar kâğıdı", "4K wallpaper")}</Button>
              {favorites.length ? <Button variant="ghost" onClick={nextFavorite}>{t("Sonraki favori", "Next favorite")}</Button> : null}
            </div>
            <div data-tour="assistant"><PlannerAssistant /></div>
          </main>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) { return <div className="space-y-1.5"><Label>{label}</Label>{children}</div>; }
function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (value: boolean) => void }) { return <div className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2"><Label className="leading-5">{label}</Label><Switch checked={checked} onCheckedChange={onChange} /></div>; }
