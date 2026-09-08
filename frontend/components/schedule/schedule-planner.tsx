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
import { apiErrorFromResponse } from "@/lib/api/fetcher";
import { ApiError, requestIdOf } from "@/lib/api/errors";
import { REQUEST_ID_HEADER, newRequestId } from "@/lib/telemetry";
import { resolvePlannerIntentStep } from "@/lib/planner-intent";
import { plannerCourseAliasMatches, plannerCourseAliases } from "@/lib/planner-course-identity";
import {
  newScheduleFlowId,
  scheduleExportTerminal,
  scheduleGenerationTerminal,
  scheduleSaveConflict,
  scheduleSourceLoadTerminal,
  scheduleStepCompleted,
  scheduleStepViewed,
} from "@/lib/schedule-telemetry";
import { usePlanning, type PlanEntry, type PlanEnvelope, type PlanState } from "@/hooks/use-planning";
import { useCampus } from "@/hooks/useCampus";
import { PlannerAssistant } from "@/components/schedule/planner-assistant";
import { PlannerIntro } from "@/components/schedule/planner-intro";
import { PlannerStepNav, type PlannerStep } from "@/components/schedule/planner-steps";
import { PlannerCalendar, type PlannerCalendarBlockChange, type PlannerCalendarEntry } from "@/components/schedule/planner-calendar";
import { formatMetuCourseCode } from "@/lib/metu-course-code";

type Day = "Mon" | "Tue" | "Wed" | "Thu" | "Fri";
// `instructor` is optional because plans saved before it existed are still in
// students' browsers and must keep loading.
type Entry = { id: string; code: string; name: string; section: string; day: Day; start: number; duration: number; startMinute?: number; durationMinutes?: number; room: string; credits: number; color: number; kind: "course" | "block"; instructor?: string };
type CatalogCourse = { code: string; name: string; credits: number; rawCode: string; required?: boolean };
type CatalogSection = { section: string; instructor: string; meetings: { day: Day; start: number; duration: number; startMinute?: number; durationMinutes?: number; room: string }[]; constraint: string; eligible?: boolean; eligibilityStatus?: "verified" | "stale" | "unavailable" | "unverified"; reason?: string; eligibilityToken?: string; eligibilityCourseCode?: string; eligibilityRawCode?: string };
type ApiCatalogSection = { section?: unknown; instructor?: unknown; meetings?: unknown; constraint?: unknown; eligible?: unknown; eligibility_status?: unknown; reason?: unknown; eligibility_token?: unknown; eligibility_course_code?: unknown; eligibility_raw_code?: unknown };
type SectionMap = Record<string, CatalogSection[]>;
// Keyed by course identity, then by section number.
type ConstraintRow = { given_dept?: string; start_char?: string; end_char?: string; min_cgpa?: string; max_cgpa?: string; min_year?: string; max_year?: string };
type SectionVerdict = { rows: ConstraintRow[]; eligible: boolean | null; eligibility_status?: "verified" | "stale" | "unavailable" | "unverified"; reason: string; eligibility_token?: string; eligibility_course_code?: string; eligibility_raw_code?: string };
type ConstraintMap = Record<string, Record<string, SectionVerdict>>;
type DepartmentOption = { code: string; name: string };
type AiPlanCourse = { code?: string; display_code?: string; name?: string; credits?: number; sections?: unknown };
type FullCurriculumCourse = { semester: number; semester_completed?: boolean; course_code: string; course_name: string; grade: string | null; status: "completed" | "failed" | "outstanding"; credits: number };
type AcademicSnapshotSummary = { term: string; enrolled_course_count: number; completed_course_count: number; fetched_at?: string | null };
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

function initialPlannerStep(): PlannerStep {
  // Keep the server and the first browser render identical. URL intent is
  // adopted after mount, once verified context and saved state are available.
  return "details";
}

function initialPlannerTerm(): string {
  // The URL term is read after mount and checked against published terms.
  return upcomingTerm();
}

function termLabel(code: string, t: (tr: string, en: string) => string) {
  const year = Number(code.slice(0, 4));
  if (!Number.isFinite(year)) return code;
  if (code.endsWith("2")) return `${year}-${year + 1} ${t("Bahar", "Spring")}`;
  if (code.endsWith("3")) return `${year + 1} ${t("Yaz Okulu", "Summer")}`;
  return `${year}-${year + 1} ${t("Güz", "Fall")}`;
}

function termDateDefaults(code: string) {
  const year = Number(code.slice(0, 4));
  if (!Number.isFinite(year)) return { start: "", end: "" };
  if (code.endsWith("2")) return { start: `${year + 1}-02-01`, end: `${year + 1}-06-30` };
  if (code.endsWith("3")) return { start: `${year + 1}-06-01`, end: `${year + 1}-08-31` };
  return { start: `${year}-09-01`, end: `${year + 1}-01-31` };
}

// Full METU codes are globally unique, so a course's identity is its whole
// code. Reducing it to the final three digits would confuse a service course
// with a home-department one: two unrelated departments both have a 201.
function courseIdentity(code: string) {
  return code.toUpperCase().replace(/[^A-Z0-9]/g, "");
}

function poolSignature(courses: CatalogCourse[]) {
  return JSON.stringify(courses.map((course) => [courseIdentity(course.rawCode), course.required !== false]));
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
      eligibilityStatus: row.eligibility_status === "verified" || row.eligibility_status === "stale" || row.eligibility_status === "unavailable" || row.eligibility_status === "unverified" ? row.eligibility_status : undefined,
      reason: String(row.reason ?? ""),
      eligibilityToken: typeof row.eligibility_token === "string" ? row.eligibility_token : undefined,
      eligibilityCourseCode: typeof row.eligibility_course_code === "string" ? row.eligibility_course_code : undefined,
      eligibilityRawCode: typeof row.eligibility_raw_code === "string" ? row.eligibility_raw_code : undefined,
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
  whatIf: boolean;
  lockedSections: Record<string, string>;
  unscheduledCourses: string[];
  generationError: string;
}): PlanState {
  return {
    entries: values.entries.map(toCanonicalEntry),
    department: values.department,
    department_label: values.departmentLabel,
    empty_days: values.emptyDays,
    avoid_conflicts: values.avoidConflicts,
    ignore_constraints: values.ignoreConstraints,
    pool: values.pool.map((course) => ({ ...course, raw_code: course.rawCode, required: course.required !== false })),
    sections: values.sections,
    alternatives: values.alternatives.map((alternative) => alternative.map(toCanonicalEntry)),
    alternative_index: values.alternativeIndex,
    favorites: values.favorites.map((favorite) => favorite.map(toCanonicalEntry)),
    favorite_index: values.favoriteIndex,
    what_if: values.whatIf,
    locked_sections: values.lockedSections,
    unscheduled_courses: values.unscheduledCourses,
    generation_error: values.generationError,
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
    eligibility_status: value.eligibility_status ?? value.eligibilityStatus ?? "unverified",
    reason: String(value.reason ?? ""),
    eligibility_token: typeof value.eligibility_token === "string" ? value.eligibility_token : String(value.eligibilityToken ?? ""),
    eligibility_course_code: typeof value.eligibility_course_code === "string" ? value.eligibility_course_code : String(value.eligibilityCourseCode ?? ""),
    eligibility_raw_code: typeof value.eligibility_raw_code === "string" ? value.eligibility_raw_code : String(value.eligibilityRawCode ?? ""),
  });
  return JSON.stringify({
    entries: state.entries.map(entry),
    department: state.department,
    department_label: state.department_label,
    empty_days: state.empty_days,
    avoid_conflicts: state.avoid_conflicts,
    ignore_constraints: state.ignore_constraints,
    what_if: state.what_if ?? state.ignore_constraints,
    locked_sections: state.locked_sections ?? {},
    unscheduled_courses: state.unscheduled_courses ?? [],
    generation_error: state.generation_error ?? "",
    pool: state.pool.map((course) => ({
      code: course.code,
      name: course.name,
      credits: course.credits,
      raw_code: course.raw_code ?? course.rawCode ?? course.code,
      required: course.required !== false,
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
        eligibilityStatus: row.eligibility_status === "verified" || row.eligibility_status === "stale" || row.eligibility_status === "unavailable" || row.eligibility_status === "unverified" ? row.eligibility_status : undefined,
        reason: String(row.reason ?? ""),
        eligibilityToken: typeof row.eligibility_token === "string" ? row.eligibility_token : undefined,
        eligibilityCourseCode: typeof row.eligibility_course_code === "string" ? row.eligibility_course_code : undefined,
        eligibilityRawCode: typeof row.eligibility_raw_code === "string" ? row.eligibility_raw_code : undefined,
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
      eligibility_status: constraints[key]?.[row.section]?.eligibility_status ?? row.eligibilityStatus ?? "unverified",
      reason: constraints[key]?.[row.section]?.reason ?? row.reason ?? "",
      eligibility_token: constraints[key]?.[row.section]?.eligibility_token ?? row.eligibilityToken,
      eligibility_course_code: constraints[key]?.[row.section]?.eligibility_course_code ?? row.eligibilityCourseCode,
      eligibility_raw_code: constraints[key]?.[row.section]?.eligibility_raw_code ?? row.eligibilityRawCode,
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

  const { connection } = useCampus();
  const [term, setTerm] = useState(() => initialPlannerTerm());
  // The selected term is always available as the current draft, but it is not
  // called a published term until the catalog endpoint returns it.
  const [publishedTerms, setPublishedTerms] = useState<string[]>([]);
  const [publishedTermsLoading, setPublishedTermsLoading] = useState(true);
  const [publishedTermsError, setPublishedTermsError] = useState<string | null>(null);
  const planning = usePlanning(term);
  const [activeStep, setActiveStep] = useState<PlannerStep>(() => initialPlannerStep());
  const [semesterDates] = useState(() => termDateDefaults(term));
  const [semesterStart, setSemesterStart] = useState(semesterDates.start);
  const [semesterEnd, setSemesterEnd] = useState(semesterDates.end);
  const [semesterDatesConfirmed, setSemesterDatesConfirmed] = useState(false);
  const [studentContext, setStudentContext] = useState<{
    department: string | null;
    surname_prefix: string | null;
    degree_level: string | null;
    year_of_study: number | null;
    program_code: string | null;
    campus: string | null;
    source: string;
    verified_at: string | null;
    confirmed_at: string | null;
  } | null>(null);
  const [academicSnapshot, setAcademicSnapshot] = useState<AcademicSnapshotSummary | null>(null);
  const [contextLoading, setContextLoading] = useState(true);
  const [contextSaving, setContextSaving] = useState(false);
  const [contextRefreshing, setContextRefreshing] = useState(false);
  const [surnameDraft, setSurnameDraft] = useState<string | null>(null);
  const [programDraft, setProgramDraft] = useState<string | null>(null);
  const [yearDraft, setYearDraft] = useState<number | null>(null);
  const [contextError, setContextError] = useState<string | null>(null);
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
  const [emptyDays, setEmptyDays] = useState<Day[]>([]);
  const [avoidConflicts, setAvoidConflicts] = useState(true);
  const [ignoreConstraints, setIgnoreConstraints] = useState(false);
  const [whatIf, setWhatIf] = useState(false);
  const [lockedSections, setLockedSections] = useState<Record<string, string>>({});
  const [unscheduledCourses, setUnscheduledCourses] = useState<string[]>([]);
  const [generationError, setGenerationError] = useState("");
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
  const [poolQuery, setPoolQuery] = useState("");
  const [suggestions, setSuggestions] = useState<CatalogCourse[]>([]);
  const [suggestBusy, setSuggestBusy] = useState(false);
  const [suggestNote, setSuggestNote] = useState("");
  const [manualOpen, setManualOpen] = useState(false);
  const [manualBusy, setManualBusy] = useState(false);
  const [draft, setDraft] = useState({ code: "", name: "", section: "1", day: "Mon" as Day, start: 9, duration: 1, room: "", credits: 3 });
  const [fullCurriculum, setFullCurriculum] = useState<FullCurriculumCourse[]>([]);
  const [fullCurriculumBusy, setFullCurriculumBusy] = useState(false);
  const [fullCurriculumWarning, setFullCurriculumWarning] = useState("");
  const [selectedEntry, setSelectedEntry] = useState<Entry | null>(null);
  // Nothing may be sent back until the canonical server state has been read,
  // or the empty first render would overwrite the plan it is about to load.
  const [hydrated, setHydrated] = useState(false);
  const [acknowledgedFingerprint, setAcknowledgedFingerprint] = useState("");
  const serverStateFingerprint = useRef("");
  const saveTracker = useRef(new PlanSaveTracker());
  const localFingerprintRef = useRef("");
  const initialConstraintsFetched = useRef(false);
  const poolRef = useRef<CatalogCourse[]>([]);
  const generationKeyRef = useRef("");
  const generationInFlightRef = useRef(false);
  const generationEpochRef = useRef(0);
  const termRef = useRef(term);
  const plannerIntentRef = useRef<{ term: string; step: PlannerStep } | null>(null);
  const [plannerIntentReady, setPlannerIntentReady] = useState(false);
  const plannerIntentResolvedRef = useRef(false);
  const scheduleFlowIdRef = useRef(newScheduleFlowId());
  const invalidateGenerationRequest = useCallback(() => {
    generationEpochRef.current += 1;
  }, []);
  const touchGenerationInputs = useCallback(() => {
    generationEpochRef.current += 1;
    generationKeyRef.current = "";
  }, []);
  const isCurrentSourceRequest = useCallback((requestTerm: string, requestEpoch: number) => (
    termRef.current === requestTerm && generationEpochRef.current === requestEpoch
  ), []);
  useEffect(() => { poolRef.current = catalogCourses; }, [catalogCourses]);
  useEffect(() => {
    // URL state is an intent, not an authority. Keep it pending until the
    // published-term source and the saved plan have loaded, then adopt only a
    // valid term and an actually reachable step.
    const params = new URLSearchParams(window.location.search);
    const requestedStep = params.get("step");
    const requestedTerm = params.get("term");
    plannerIntentRef.current = {
      step: requestedStep === "courses" || requestedStep === "timetable" ? requestedStep : "details",
      term: requestedTerm && /^20\d{2}[1-3]$/.test(requestedTerm) ? requestedTerm : term,
    };
    const intentTimer = window.setTimeout(() => setPlannerIntentReady(true), 0);
    return () => window.clearTimeout(intentTimer);
  // The intent belongs to the first URL that opened this planner. Later term
  // changes are explicit user actions and must not rewrite a still-pending
  // deep link.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    termRef.current = term;
    scheduleFlowIdRef.current = newScheduleFlowId();
    saveTracker.current.reset(term);
    serverStateFingerprint.current = "";
    generationEpochRef.current += 1;
    generationKeyRef.current = "";
    poolRef.current = [];
    initialConstraintsFetched.current = false;
    const dates = termDateDefaults(term);
    const timer = window.setTimeout(() => {
      setHydrated(false);
      setEntries([]);
      setCatalogCourses([]);
      setSectionsByCourse({});
      setConstraints({});
      setAlternatives([]);
      setAlternativeIndex(0);
      setLockedSections({});
      setUnscheduledCourses([]);
      setGenerationError("");
      setSemesterStart(dates.start);
      setSemesterEnd(dates.end);
      setSemesterDatesConfirmed(false);
      setSelectedEntry(null);
      if (plannerIntentResolvedRef.current) {
        setActiveStep((current) => current === "details" ? "details" : "courses");
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [term]);
  useEffect(() => {
    let cancelled = false;
    const loadFlowId = newScheduleFlowId();
    const timer = window.setTimeout(() => {
      void jsonFetch<{ semesters?: string[]; source?: string }>("/api/schedule/semesters")
        .then((response) => {
          if (cancelled) return;
          // A fallback current term is useful for the API's error response but
          // is not evidence that the catalog published an offering. Keep it
          // out of the selector until the authoritative source answers.
          const terms = response.source === "catalog"
            ? (response.semesters ?? []).filter((value) => /^20\d{2}[1-3]$/.test(value))
            : [];
          scheduleSourceLoadTerminal("catalog", terms.length ? "success" : "incomplete", loadFlowId);
          const published = [...new Set(terms)].sort().reverse();
          setPublishedTerms(published);
          if (response.source !== "catalog") setPublishedTermsError(t("Katalog dönemleri şu anda doğrulanamıyor.", "Catalog terms are not currently verifiable."));
          setPublishedTermsLoading(false);
          const requested = plannerIntentRef.current?.term;
          const nextTerm = requested && published.includes(requested)
            ? requested
            : published.includes(term)
              ? term
              : published[0] ?? "";
          if (nextTerm && plannerIntentRef.current) {
            plannerIntentRef.current = { ...plannerIntentRef.current, term: nextTerm };
          }
          if (nextTerm && nextTerm !== term) setTerm(nextTerm);
          if (nextTerm && typeof window !== "undefined") {
            const url = new URL(window.location.href);
            url.searchParams.set("term", nextTerm);
            window.history.replaceState({}, "", url);
          }
        })
        .catch((error) => {
          scheduleSourceLoadTerminal("catalog", "unavailable", loadFlowId);
          if (!cancelled) {
            setPublishedTerms([]);
            setPublishedTermsError(error instanceof Error ? error.message : t("Yayınlanan dönemler alınamadı.", "Published terms could not be loaded."));
            setPublishedTermsLoading(false);
          }
        });
    }, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  // The catalog is the authority for which terms can be planned. Refetching it
  // for every selector change would re-open a race and briefly accept a stale
  // URL term, so this source is loaded once per planner mount.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  const busy = planBusy || generateProgress !== null;

  useEffect(() => {
    scheduleStepViewed(activeStep, scheduleFlowIdRef.current);
  }, [activeStep]);

  const verifiedCampus = Boolean(connection?.connected && connection.verified_at);
  const verifiedAcademicContext = Boolean(studentContext?.verified_at);
  const detailsReady = verifiedCampus && verifiedAcademicContext;
  const contextDraftValid = /^[A-ZÇĞİÖŞÜ]{2}$/i.test((surnameDraft ?? "").trim());
  const termIsPublished = publishedTerms.includes(term);
  const hasCourses = catalogCourses.length > 0 || entries.some((entry) => entry.kind === "course");
  // Account and verified SAIS context are required before either branch can
  // read courses. What-if changes the eligibility policy after that gate; it
  // never becomes a way around identity or campus verification.
  const canExplore = detailsReady && termIsPublished;
  const canOpen = useMemo<Record<PlannerStep, boolean>>(() => ({
    details: true,
    courses: canExplore,
    timetable: canExplore && hasCourses,
  }), [canExplore, hasCourses]);
  const completed = useMemo<Record<PlannerStep, boolean>>(() => ({
    details: detailsReady,
    courses: hasCourses,
    timetable: entries.length > 0,
  }), [detailsReady, entries.length, hasCourses]);

  useEffect(() => {
    if (detailsReady) scheduleStepCompleted("details", "success", scheduleFlowIdRef.current);
  }, [detailsReady]);

  useEffect(() => {
    const intent = plannerIntentRef.current;
    if (
      !plannerIntentReady
      || plannerIntentResolvedRef.current
      || contextLoading
      || planning.loading
      || !planning.ready
      || !hydrated
      || publishedTermsLoading
      || !intent
      || intent.term !== term
    ) return;
    const next = resolvePlannerIntentStep(intent.step, canOpen, planning.ready, hydrated);
    if (!next) return;
    plannerIntentResolvedRef.current = true;
    plannerIntentRef.current = null;
    setActiveStep(next);
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.set("step", next);
      url.searchParams.set("term", term);
      window.history.replaceState({}, "", url);
    }
  }, [canOpen, contextLoading, hydrated, plannerIntentReady, planning.loading, planning.ready, publishedTermsLoading, term]);

  useEffect(() => {
    if (planning.conflict) scheduleSaveConflict(newScheduleFlowId());
  }, [planning.conflict]);

  const navigateStep = useCallback((next: PlannerStep, replace = false) => {
    if (!canOpen[next]) return;
    setActiveStep(next);
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.set("step", next);
      url.searchParams.set("term", term);
      window.history[replace ? "replaceState" : "pushState"]({}, "", url);
    }
  }, [canOpen, term]);

  useEffect(() => {
    if (!plannerIntentReady || !plannerIntentResolvedRef.current) return;
    if (canOpen[activeStep]) return;
    const fallback: PlannerStep = activeStep === "timetable" && canOpen.courses ? "courses" : "details";
    const timer = window.setTimeout(() => navigateStep(fallback, true), 0);
    return () => window.clearTimeout(timer);
  }, [activeStep, canOpen, navigateStep, plannerIntentReady]);

  useEffect(() => {
    const onPopState = () => {
      const params = new URLSearchParams(window.location.search);
      const requestedTerm = params.get("term");
      if (requestedTerm && publishedTerms.includes(requestedTerm)) setTerm(requestedTerm);
      const value = params.get("step");
      const next: PlannerStep = value === "courses" || value === "timetable" ? value : "details";
      setActiveStep(canOpen[next] ? next : "details");
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [canOpen, publishedTerms]);

  useEffect(() => {
    let cancelled = false;
    const loadFlowId = newScheduleFlowId();
    void Promise.all([
      jsonFetch<{ department: string | null; surname_prefix: string | null; degree_level: string | null; year_of_study: number | null; program_code: string | null; campus: string | null; source: string; verified_at: string | null; confirmed_at: string | null }>("/api/student/context"),
      jsonFetch<{ context?: typeof studentContext; snapshots?: AcademicSnapshotSummary[] }>("/api/student/academic-data"),
    ]).then(([context, academic]) => {
      if (cancelled) return;
      scheduleSourceLoadTerminal("context", "success", loadFlowId);
      setStudentContext(academic.context ?? context);
      setAcademicSnapshot(academic.snapshots?.[0] ?? null);
      setSurnameDraft((academic.context ?? context)?.surname_prefix ?? null);
      setProgramDraft((academic.context ?? context)?.program_code ?? null);
      setYearDraft((academic.context ?? context)?.year_of_study ?? null);
    }).catch((error) => {
      if (!cancelled) {
        scheduleSourceLoadTerminal("context", "error", loadFlowId);
        setContextError(error instanceof Error ? error.message : t("Akademik bilgiler alınamadı.", "Academic details could not be loaded."));
      }
    }).finally(() => {
      if (!cancelled) setContextLoading(false);
    });
    return () => { cancelled = true; };
  }, [t]);

  async function saveStudentContext() {
    setContextSaving(true);
    setContextError(null);
    try {
      const next = await jsonFetch<typeof studentContext>("/api/student/context", {
        method: "PUT",
        body: {
          surname_prefix: surnameDraft?.trim().slice(0, 2) || null,
          program_code: programDraft?.trim() || null,
          year_of_study: yearDraft,
        },
      });
      setStudentContext(next);
      setSurnameDraft(next?.surname_prefix ?? null);
      setProgramDraft(next?.program_code ?? null);
      setYearDraft(next?.year_of_study ?? null);
      toast.success(t("Akademik bağlamın kaydedildi.", "Your academic context was saved."));
    } catch (error) {
      setContextError(error instanceof Error ? error.message : t("Akademik bağlam kaydedilemedi.", "Academic context could not be saved."));
    } finally {
      setContextSaving(false);
    }
  }

  async function refreshStudentContext() {
    if (!verifiedCampus) {
      setContextError(t("Önce ODTÜ bağlantını doğrula.", "Verify your METU connection first."));
      return;
    }
    setContextRefreshing(true);
    setContextError(null);
    const flowId = newScheduleFlowId();
    try {
      const response = await jsonFetch<{ context?: typeof studentContext; snapshots?: AcademicSnapshotSummary[] }>("/api/student/academic-data/sync", {
        method: "POST",
        body: { term, force: true },
      });
      const next = response.context ?? null;
      setStudentContext(next);
      setAcademicSnapshot(response.snapshots?.[0] ?? null);
      setSurnameDraft(next?.surname_prefix ?? null);
      setProgramDraft(next?.program_code ?? null);
      setYearDraft(next?.year_of_study ?? null);
      scheduleSourceLoadTerminal("context", next?.verified_at ? "success" : "incomplete", flowId);
      if (next?.verified_at) {
        toast.success(t("SAIS akademik bağlamın yenilendi.", "Your SAIS academic context was refreshed."));
      } else {
        setContextError(t("SAIS bağlamı doğrulanamadı. Bağlantını kontrol edip tekrar dene.", "SAIS could not verify your academic context. Check the connection and try again."));
      }
    } catch (error) {
      scheduleSourceLoadTerminal("context", "error", flowId);
      captureRequestFailure(error, { operation: "schedule.context_refresh", kind: "mutation" });
      setContextError(error instanceof Error ? error.message : t("SAIS akademik bağlamı yenilenemedi.", "SAIS academic context could not be refreshed."));
    } finally {
      setContextRefreshing(false);
    }
  }

  // --- persistence --------------------------------------------------------
  const applyCanonicalState = useCallback((state: PlanState) => {
    const nextEntries = state.entries.map((entry) => ({ ...fromCanonicalEntry(entry), code: formatMetuCourseCode(entry.code) }));
    const nextPool = state.pool.map((course) => ({
      code: formatMetuCourseCode(course.code),
      name: course.name,
      credits: course.credits,
      rawCode: course.raw_code ?? course.rawCode ?? course.code,
      required: course.required !== false,
    }));
    const nextSections = fromCanonicalSections(state.sections);
    poolRef.current = nextPool;
    const nextAlternatives = state.alternatives.map((alternative) => alternative.map((entry) => ({ ...fromCanonicalEntry(entry), code: formatMetuCourseCode(entry.code) })));
    const nextFavorites = state.favorites.map((favorite) => favorite.map((entry) => ({ ...fromCanonicalEntry(entry), code: formatMetuCourseCode(entry.code) })));
    setEntries(nextEntries);
    setCatalogCourses(nextPool);
    setSectionsByCourse(nextSections);
    setDepartment(state.department);
    setDepartmentLabel(state.department_label);
    setEmptyDays(state.empty_days.filter((day): day is Day => DAYS.includes(day)));
    setAvoidConflicts(state.avoid_conflicts);
    setIgnoreConstraints(state.ignore_constraints);
    setWhatIf(state.what_if ?? state.ignore_constraints);
    setLockedSections(state.locked_sections ?? {});
    setUnscheduledCourses(state.unscheduled_courses ?? []);
    setGenerationError(state.generation_error ?? "");
    setAlternatives(nextAlternatives);
    setAlternativeIndex(state.alternative_index);
    setFavorites(nextFavorites);
    setFavoriteIndex(state.favorite_index);
    const fingerprint = canonicalStateFingerprint(state);
    serverStateFingerprint.current = fingerprint;
    setAcknowledgedFingerprint(fingerprint);
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
    departmentLabel,
    emptyDays,
    avoidConflicts,
    ignoreConstraints,
    pool: catalogCourses,
    sections: canonicalSections as SectionMap,
    alternatives,
    alternativeIndex,
    favorites,
    favoriteIndex,
    whatIf,
    lockedSections,
    unscheduledCourses,
    generationError,
  }), [
    alternatives,
    alternativeIndex,
    avoidConflicts,
    canonicalSections,
    catalogCourses,
    department,
    departmentLabel,
    emptyDays,
    entries,
    favoriteIndex,
    favorites,
    ignoreConstraints,
    lockedSections,
    whatIf,
    unscheduledCourses,
    generationError,
  ]);

  const localFingerprint = useMemo(() => canonicalStateFingerprint(localCanonicalState), [localCanonicalState]);
  useEffect(() => {
    localFingerprintRef.current = localFingerprint;
  }, [localFingerprint]);

  const autoGenerationKey = useMemo(() => JSON.stringify({
    courses: catalogCourses.map((course) => [course.rawCode, course.required !== false]),
    emptyDays,
    avoidConflicts,
    whatIf,
    lockedSections,
  }), [avoidConflicts, catalogCourses, emptyDays, lockedSections, whatIf]);

  const reconcileResponse = useCallback((next: PlanEnvelope, fingerprint: string) => {
    scheduleSourceLoadTerminal("saved", "success", newScheduleFlowId());
    const decision = saveTracker.current.acknowledge(next, fingerprint);
    if (decision === "ignore") return;
    if (decision === "adopt") applyCanonicalState(next.state);
    else {
      const fingerprint = canonicalStateFingerprint(next.state);
      serverStateFingerprint.current = fingerprint;
      setAcknowledgedFingerprint(fingerprint);
    }
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

  const selectCalendarEntry = useCallback((entry: PlannerCalendarEntry) => {
    setSelectedEntry(entries.find((candidate) => candidate.id === entry.id) ?? {
      ...entry,
      start: Math.floor(itemStartMinute(entry) / 60),
      duration: Math.max(1, Math.ceil(itemDurationMinutes(entry) / 60)),
      startMinute: itemStartMinute(entry),
      durationMinutes: itemDurationMinutes(entry),
      credits: 0,
      color: 0,
    });
  }, [entries]);

  const changePersonalBlock = useCallback((change: PlannerCalendarBlockChange) => {
    const current = entries.find((entry) => entry.id === change.id);
    if (!current || current.kind !== "block") return;
    const nextEntry: Entry = {
      ...current,
      day: change.day,
      start: Math.floor(change.startMinute / 60),
      duration: Math.max(1, Math.ceil(change.durationMinutes / 60)),
      startMinute: change.startMinute,
      durationMinutes: change.durationMinutes,
    };
    if (emptyDays.includes(nextEntry.day)) {
      toast.error(t("Bu gün boş gün olarak seçili.", "This day is marked as empty."));
      return;
    }
    const nextEntries = entries.map((entry) => entry.id === change.id ? nextEntry : entry);
    if (avoidConflicts && nextEntries.some((entry) => entry.id !== nextEntry.id && overlaps(entry, nextEntry))) {
      toast.error(t("Bu kişisel blok mevcut programla çakışıyor.", "This personal block conflicts with the current schedule."));
      return;
    }
    invalidateGenerationRequest();
    setEntries(nextEntries);
    setSelectedEntry(nextEntry);
    const request = submitPlanningUpdate({
      operation: "regenerate",
      entries: nextEntries.map(toCanonicalEntry),
      pool: catalogCourses.map((course) => ({
        code: course.code,
        name: course.name,
        credits: course.credits,
        raw_code: course.rawCode,
        required: course.required !== false,
      })),
      sections: canonicalSectionsFromLocal(sectionsByCourse, constraints),
      empty_days: emptyDays,
      avoid_conflicts: avoidConflicts,
      what_if: whatIf,
      locked_sections: lockedSections,
    });
    void request.then((next) => {
      if (!next) setGenerationError(t("Kişisel blok kaydedilemedi; son başarılı program korunuyor.", "The personal block could not be saved; the last successful schedule is preserved."));
    });
  }, [avoidConflicts, catalogCourses, constraints, emptyDays, entries, invalidateGenerationRequest, lockedSections, sectionsByCourse, submitPlanningUpdate, t, whatIf]);

  // Every planner edit is persisted as one canonical state update. This is a
  // short debounce for rapid UI changes, while the server still orders each
  // request with its revision and idempotency key.
  useEffect(() => {
    if (!hydrated || !planningReady || planning.saving || planning.retryable || planning.conflict) return;
    const fingerprint = localFingerprint;
    if (fingerprint === serverStateFingerprint.current) return;
    // Course-pool and solver-control edits are committed by the single
    // regenerate command below. Letting this generic draft writer run first
    // would create an undo revision for the inputs and a second one for the
    // generated result.
    // Step 2 is an editable draft. Persist its course pool and section choices
    // so a reload resumes there. Only suppress this writer while step 3 is
    // about to run the atomic regenerate operation; that keeps a timetable
    // edit to one undo revision without losing a course-step draft.
    if (activeStep === "timetable" && catalogCourses.length && generationKeyRef.current !== autoGenerationKey) return;
    const timer = window.setTimeout(() => {
      const submitted: SubmittedMutation = { term, fingerprint, idempotencyKey: crypto.randomUUID() };
      saveTracker.current.submit(submitted.idempotencyKey, submitted.term, submitted.fingerprint);
      void planningUpdate({ operation: "replace", state: localCanonicalState }, submitted.idempotencyKey);
    }, 350);
    return () => window.clearTimeout(timer);
  }, [activeStep, autoGenerationKey, catalogCourses.length, hydrated, localCanonicalState, localFingerprint, planning.conflict, planning.retryable, planning.saving, planningIdentity, planningReady, planningUpdate, term]);

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
  const scheduledCodes = new Set(entries.filter((entry) => entry.kind === "course").map((entry) => courseIdentity(entry.code)));
  const selectedPoolCount = catalogCourses.filter((course) =>
    scheduledCodes.has(courseIdentity(course.code)) || scheduledCodes.has(courseIdentity(course.rawCode))).length;
  const dayLabel = useCallback((day: Day) => ({ Mon: t("Pzt", "Mon"), Tue: t("Sal", "Tue"), Wed: t("Çar", "Wed"), Thu: t("Per", "Thu"), Fri: t("Cum", "Fri") })[day], [t]);
  // What distinguishes one alternative from the next, in the terms a student
  // is choosing on: which days it leaves free.
  const currentShape = useMemo(() => (entries.length ? scheduleShape(entries) : null), [entries]);
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
  const curriculumSummary = useMemo(() => ({
    completed: fullCurriculum.filter((course) => course.status === "completed").length,
    failed: fullCurriculum.filter((course) => course.status === "failed").length,
    outstanding: fullCurriculum.filter((course) => course.status === "outstanding").length,
  }), [fullCurriculum]);
  // Export only the acknowledged canonical revision. A debounce can leave a
  // local edit visible for a few hundred milliseconds, and exporting that
  // draft would produce a file the server cannot reproduce or validate.
  const exportBlocked = !hydrated
    || localFingerprint !== acknowledgedFingerprint
    || busy
    || planning.saving
    || planning.retryable
    || Boolean(planning.conflict)
    || Boolean(planning.saveError)
    || Boolean(generationError)
    || unscheduledCourses.length > 0;
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
    if (next.kind === "course" && !section && !whatIf) {
      return toast.error(t("Normal programda ders şubesi gerekli.", "A course section is required in a normal plan."));
    }
    let verdict: SectionVerdict | undefined;
    if (next.kind === "course" && section) {
      setManualBusy(true);
      try {
        const verdicts = await loadConstraints(code);
        verdict = verdicts[section];
        const verified = verdict?.eligibility_status === "verified" && verdict.eligible === true;
        if (!whatIf && !verified) {
          const reason = localizedRestrictionReason(verdict?.reason ?? "", t);
          return toast.error(verdict?.reason
            ? t(`Şube ${section} doğrulanamadı veya sana kapalı: ${reason}. Keşif modunu açarak ekleyebilirsin.`,
                `Section ${section} is unverified or closed to you: ${reason}. Turn on what-if mode to explore it.`)
            : t(`Şube ${section} için uygunluk doğrulanamadı.`, `Eligibility for section ${section} could not be verified.`));
        }
        if (!verified) {
          toast.warning(t(`Şube ${section} keşif modunda eklendi; kayıt uygunluğunu ODTÜ'de doğrula.`,
            `Section ${section} was added in what-if mode; verify registration eligibility with METU.`));
        }
      } finally {
        setManualBusy(false);
      }
    }

    // Keep a manually entered section's server verdict beside its meeting so
    // the debounced canonical replace carries the same evidence as a catalog
    // selection. Without this, the UI checked the section but the normal-plan
    // validator only saw a bare entry on the next save.
    if (next.kind === "course" && section) {
      const identity = courseIdentity(code);
      setSectionsByCourse((current) => ({
        ...current,
        [identity]: [
          ...(current[identity] ?? []).filter((item) => item.section !== section),
          {
            section,
            instructor: "",
            meetings: [{ day: next.day, start: next.start, duration: next.duration, startMinute: itemStartMinute(next), durationMinutes: itemDurationMinutes(next), room: next.room }],
            constraint: "",
            eligible: verdict?.eligible === true ? true : verdict?.eligible === false ? false : undefined,
            eligibilityStatus: verdict?.eligibility_status ?? "unverified",
            reason: verdict?.reason ?? "",
            eligibilityToken: verdict?.eligibility_token,
            eligibilityCourseCode: verdict?.eligibility_course_code,
            eligibilityRawCode: verdict?.eligibility_raw_code,
          },
        ],
      }));
    }

    invalidateGenerationRequest();
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
    touchGenerationInputs();
    setCatalogCourses((current) => {
      const next = [...current, chosen];
      poolRef.current = next;
      return next;
    });
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
    const aliases = plannerCourseAliases(course);
    touchGenerationInputs();
    setCatalogCourses((current) => {
      const next = current.filter((item) => !plannerCourseAliasMatches(item.code, aliases) && !plannerCourseAliasMatches(item.rawCode, aliases));
      poolRef.current = next;
      return next;
    });
    setSectionsByCourse((current) => {
      const next = { ...current };
      for (const key of Object.keys(next)) if (plannerCourseAliasMatches(key, aliases)) delete next[key];
      return next;
    });
    setConstraints((current) => {
      const next = { ...current };
      for (const key of Object.keys(next)) if (plannerCourseAliasMatches(key, aliases)) delete next[key];
      return next;
    });
    setEntries((current) => current.filter((entry) => entry.kind === "block" || !plannerCourseAliasMatches(entry.code, aliases)));
    setLockedSections((current) => {
      const next = { ...current };
      for (const key of Object.keys(next)) if (plannerCourseAliasMatches(key, aliases)) delete next[key];
      return next;
    });
    setAlternatives([]);
    setAlternativeIndex(0);
    setUnscheduledCourses([]);
    setGenerationError("");
    setSelectedEntry((current) => current && plannerCourseAliasMatches(current.code, aliases) ? null : current);
    if (plannerCourseAliasMatches(expandedCourse, aliases)) setExpandedCourse(null);
  }

  function clearCoursePool() {
    touchGenerationInputs();
    poolRef.current = [];
    setCatalogCourses([]);
    setSectionsByCourse({});
    setConstraints({});
    setExpandedCourse(null);
    setCatalogSearch("");
    setCurriculumNotice("");
    // The alternatives were arrangements of the pool that just went away.
    setAlternatives([]);
    setAlternativeIndex(0);
    setEntries((current) => current.filter((entry) => entry.kind === "block"));
    setLockedSections({});
    setUnscheduledCourses([]);
    setGenerationError("");
    setSelectedEntry((current) => current?.kind === "block" ? current : null);
    toast.success(t("Ders havuzu temizlendi.", "Course pool cleared."));
  }

  function removeScheduledCourse(courseEntries: Entry[]) {
    const ids = new Set(courseEntries.map((entry) => entry.id));
    const course = catalogCourses.find((candidate) => courseIdentity(candidate.code) === courseIdentity(courseEntries[0]?.code ?? "") || courseIdentity(candidate.rawCode) === courseIdentity(courseEntries[0]?.code ?? ""));
    if (course) {
      removePoolCourse(course);
      return;
    }
    invalidateGenerationRequest();
    setEntries((current) => current.filter((entry) => !ids.has(entry.id)));
  }

  // --- campus data --------------------------------------------------------

  /** This course's sections, fetched from the catalog when we hold none yet. */
  const fetchSections = useCallback(async (course: CatalogCourse, known: SectionMap): Promise<CatalogSection[]> => {
    const identity = courseIdentity(course.rawCode);
    const cached = known[identity];
    if (cached?.length) return cached;
    const loadFlowId = newScheduleFlowId();
    const requestTerm = term;
    const requestEpoch = generationEpochRef.current;
    const courseDepartment = owningDepartment(course.rawCode, department);
    let response: { sections?: unknown };
    try {
      response = await jsonFetch<{ sections?: unknown }>(`/api/schedule/courses/${encodeURIComponent(course.rawCode)}?department=${encodeURIComponent(courseDepartment)}&semester=${encodeURIComponent(term)}`);
    } catch (error) {
      scheduleSourceLoadTerminal("sections", "error", loadFlowId);
      throw error;
    }
    if (!isCurrentSourceRequest(requestTerm, requestEpoch)) return [];
    const sections = fromTypedSections(response.sections);
    scheduleSourceLoadTerminal("sections", sections.length ? "success" : "incomplete", loadFlowId);
    setSectionsByCourse((current) => {
      if (!isCurrentSourceRequest(requestTerm, requestEpoch) || !poolRef.current.some((item) => courseIdentity(item.rawCode) === identity)) return current;
      return { ...current, [identity]: sections };
    });
    return sections;
  }, [department, isCurrentSourceRequest, term]);

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
    const requestTerm = term;
    const requestEpoch = generationEpochRef.current;
    let response: { courses?: Record<string, { sections?: unknown; error?: string }> };
    try {
      response = await jsonFetch<{ courses?: Record<string, { sections?: unknown; error?: string }> }>(
        "/api/schedule/sections",
        { method: "POST", body: { semester: requestTerm, department: department.trim() || undefined, courses: wanted.map((course) => course.rawCode) } },
      );
    } catch (error) {
      if (!isCurrentSourceRequest(requestTerm, requestEpoch)) return known;
      throw error;
    }
    if (!isCurrentSourceRequest(requestTerm, requestEpoch)) return known;
    const found: SectionMap = {};
    for (const [rawCode, payload] of Object.entries(response.courses ?? {})) {
      if (payload?.error !== undefined) continue;
      found[courseIdentity(rawCode)] = fromTypedSections(payload?.sections);
    }
    const activeFound = Object.fromEntries(
      Object.entries(found).filter(([identity]) => poolRef.current.some((course) => courseIdentity(course.rawCode) === identity)),
    );
    setSectionsByCourse((current) => isCurrentSourceRequest(requestTerm, requestEpoch) ? ({ ...current, ...activeFound }) : current);
    return { ...known, ...activeFound };
  }, [department, isCurrentSourceRequest, term]);

  /**
   * Verdicts for a whole pool of courses in one request.
   *
   * Restrictions differ from section to section, which is the normal case
   * rather than the exception, so a student cannot be told whether a course is
   * open to them until every section's table has been read. Fetching that per
   * course from here meant one request each; the broker does them together
   * over a single catalog connection instead.
   */
  const fetchAllConstraints = useCallback(async (courses: CatalogCourse[]): Promise<ConstraintMap> => {
    const wanted = courses.map((course) => course.rawCode).filter(Boolean);
    if (!wanted.length) return {};
    const loadFlowId = newScheduleFlowId();
    const requestTerm = term;
    const requestEpoch = generationEpochRef.current;
    setConstraintsBusy(true);
    const collected: ConstraintMap = {};
    let failed = false;
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
            { method: "POST", body: { semester: requestTerm, department: department.trim() || undefined, courses: chunk } },
          );
          if (!isCurrentSourceRequest(requestTerm, requestEpoch)) continue;
          setConstraints((current) => {
            if (!isCurrentSourceRequest(requestTerm, requestEpoch)) return current;
            const updated = { ...current };
            for (const [rawCode, payload] of Object.entries(response.courses ?? {})) {
              const identity = courseIdentity(rawCode);
              // Ignore if the course was removed from the pool during the request
              if (!poolRef.current.some((c) => courseIdentity(c.rawCode) === identity)) continue;
              const sections = payload?.sections ?? {};
              // Never replace verdicts we already hold with an empty answer
              if (!Object.keys(sections).length && current[identity] && Object.keys(current[identity]).length) continue;
              collected[identity] = sections;
              updated[identity] = sections;
            }
            return updated;
          });
        } catch {
          failed = true;
          // One failed group must not cost the rest their verdicts. A course
          // with none remains explicitly unverified and is blocked in a
          // normal plan until the student chooses what-if mode.
        }
      }
    } finally {
      setConstraintsBusy(false);
      scheduleSourceLoadTerminal("constraints", Object.keys(collected).length ? (failed ? "incomplete" : "success") : "unavailable", loadFlowId);
    }
    return collected;
  }, [department, isCurrentSourceRequest, term]);
  const fetchAllConstraintsRef = useRef(fetchAllConstraints);
  useEffect(() => {
    fetchAllConstraintsRef.current = fetchAllConstraints;
  }, [fetchAllConstraints]);

  /**
   * The eligibility verdicts for one course, fetched once and remembered.
   *
   * Returns them as well as storing them, because a manual add has to decide
   * *now* whether to accept the entry and cannot wait for a state update.
   */
  const loadConstraints = useCallback(async (rawCode: string): Promise<Record<string, SectionVerdict>> => {
    const identity = courseIdentity(rawCode);
    const courseDepartment = owningDepartment(rawCode, department);
    const loadFlowId = newScheduleFlowId();
    const requestTerm = term;
    const requestEpoch = generationEpochRef.current;
    try {
      const response = await jsonFetch<{ sections?: Record<string, SectionVerdict> }>(
        `/api/schedule/courses/${encodeURIComponent(rawCode)}/constraints?department=${encodeURIComponent(courseDepartment)}&semester=${encodeURIComponent(requestTerm)}`,
      );
      if (!isCurrentSourceRequest(requestTerm, requestEpoch)) return {};
      const sections = response.sections ?? {};
      // Never replace verdicts we already hold with an empty answer: the bulk
      // fetch and this one race, and an empty overwrite silently turned every
      // red flag back to "unrestricted".
      setConstraints((current) => (
        !isCurrentSourceRequest(requestTerm, requestEpoch) ? current : (
        !Object.keys(sections).length && current[identity] && Object.keys(current[identity]).length
          ? current
          : { ...current, [identity]: sections }
        )
      ));
      scheduleSourceLoadTerminal("constraints", Object.keys(sections).length ? "success" : "incomplete", loadFlowId);
      return sections;
    } catch {
      if (!isCurrentSourceRequest(requestTerm, requestEpoch)) return {};
      // A course whose table cannot be read keeps its sections and simply gets
      // no verdict. The eligibility check treats that as unknown, never as an
      // implicit approval.
      setConstraints((current) => (current[identity] ? current : { ...current, [identity]: {} }));
      scheduleSourceLoadTerminal("constraints", "error", loadFlowId);
      return {};
    }
  }, [department, isCurrentSourceRequest, term]);

  const fetchConstraints = (course: CatalogCourse) => loadConstraints(course.rawCode);

  useEffect(() => {
    if (hydrated && !initialConstraintsFetched.current && catalogCourses.length) {
      initialConstraintsFetched.current = true;
      void fetchAllConstraints(catalogCourses);
    }
  }, [hydrated, catalogCourses, fetchAllConstraints]);

  /** Display the server's eligibility verdict; the browser never recomputes it. */
  const sectionAllowed = useCallback((course: CatalogCourse, section: CatalogSection) => {
    const verdict = constraints[courseIdentity(course.rawCode)]?.[section.section];
    const status = verdict?.eligibility_status ?? section.eligibilityStatus ?? "unverified";
    const eligible = verdict?.eligible ?? section.eligible ?? null;
    const reason = verdict?.reason ?? section.reason ?? "";
    return {
      allowed: whatIf || (status === "verified" && eligible === true),
      status,
      reason: reason || (status === "verified" ? "" : t("Bu şubenin uygunluğu henüz doğrulanmadı.", "This section's eligibility has not been verified yet.")),
    };
  }, [constraints, t, whatIf]);

  async function requestCurriculum(courses: CatalogCourse[]) {
    // This was the slowest thing a student waited on in the whole app — a
    // median of 95.8 seconds — because it ran an agent that called one catalog
    // tool per department with a model turn between each. It now reads the
    // curriculum directly and answers in about two.
    const startedAt = Date.now();
    const loadFlowId = newScheduleFlowId();
    const requestTerm = term;
    const requestEpoch = generationEpochRef.current;
    let response: { courses?: AiPlanCourse[]; warnings?: string[]; prerequisite_rejections?: PrerequisiteRejection[]; cache_hit?: boolean; duration_ms?: number };
    try {
      response = await jsonFetch<{ courses?: AiPlanCourse[]; warnings?: string[]; prerequisite_rejections?: PrerequisiteRejection[]; cache_hit?: boolean; duration_ms?: number }>("/api/schedule/curriculum", {
        method: "POST",
        // Omitted rather than sent empty: the broker reads the department from
        // the stored campus context when the client has none, so a page whose
        // own state is empty still gets a plan instead of a validation error.
        body: { department: department.trim() || undefined, semester: term, courses: courses.map((course) => ({ code: course.rawCode })) },
      });
    } catch (error) {
      scheduleSourceLoadTerminal("curriculum", "error", loadFlowId);
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
        required: true,
      }];
    });
    if (!isCurrentSourceRequest(requestTerm, requestEpoch)) return { courses: [], warnings: [], prerequisiteRejections: [], cacheHit: response.cache_hit, durationMs: response.duration_ms };
    setSectionsByCourse((current) => isCurrentSourceRequest(requestTerm, requestEpoch) ? ({ ...current, ...sectionMap }) : current);
    const warnings = (response.warnings ?? []).filter((warning): warning is string => typeof warning === "string");
    scheduleSourceLoadTerminal("curriculum", warnings.length && !verified.length ? "incomplete" : "success", loadFlowId);
    scheduleStepCompleted("courses", verified.length ? "success" : "incomplete", loadFlowId);
    captureProductEvent("schedule_plan_completed", {
      result: "success",
      requested_courses: courses.length,
      returned_courses: verified.length,
      warnings: warnings.length,
      duration_seconds: (Date.now() - startedAt) / 1000,
    });
    return { courses: verified, warnings, prerequisiteRejections: response.prerequisite_rejections ?? [], cacheHit: response.cache_hit, durationMs: response.duration_ms };
  }

  async function loadFullCurriculum() {
    setFullCurriculumBusy(true);
    setFullCurriculumWarning("");
    const loadFlowId = newScheduleFlowId();
    const requestTerm = term;
    const requestEpoch = generationEpochRef.current;
    try {
      const response = await jsonFetch<{ courses?: FullCurriculumCourse[]; warning?: string | null }>("/api/schedule/curriculum/full", {
        method: "POST",
        body: { department: department.trim() || undefined, semester: term, courses: [] },
      });
      if (!isCurrentSourceRequest(requestTerm, requestEpoch)) return;
      setFullCurriculum(response.courses ?? []);
      setFullCurriculumWarning(response.warning ?? "");
      scheduleSourceLoadTerminal("curriculum", response.courses?.length ? "success" : "incomplete", loadFlowId);
    } catch (error) {
      scheduleSourceLoadTerminal("curriculum", "error", loadFlowId);
      captureRequestFailure(error, { operation: "schedule.full_curriculum", kind: "query" });
      setFullCurriculumWarning(error instanceof Error ? error.message : t("Müfredat alınamadı.", "The curriculum could not be loaded."));
    } finally {
      setFullCurriculumBusy(false);
    }
  }

  async function loadRequiredCourses() {
    // No department gate any more. The broker resolves it from the stored
    // campus context when the request omits it, and answers with a specific
    // 422 when it genuinely has none — which is more than this check knew.
    setPlanBusy(true); setCurriculumNotice(""); setExpandedCourse(null);
    const requestTerm = term;
    const requestEpoch = generationEpochRef.current;
    try {
      const result = await requestCurriculum([]);
      if (!isCurrentSourceRequest(requestTerm, requestEpoch)) return;
      touchGenerationInputs();
      poolRef.current = result.courses;
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
  const generateSchedule = useCallback(async (flow: "automatic" | "manual" = "automatic"): Promise<boolean> => {
    if (!catalogCourses.length) { toast.error(t("Önce dönem derslerini getir.", "Load semester courses first.")); return false; }
    if (!planning.ready) { toast.error(t("Program sunucusu henüz hazır değil.", "The planner server is still loading.")); return false; }
    if (planning.saving || planning.retryable || planning.conflict) { toast.error(t("Önce bekleyen program kaydını tamamla.", "Finish the pending schedule save first.")); return false; }
    const requestedTerm = term;
    const requestedEpoch = generationEpochRef.current;
    const requestedPool = [...catalogCourses];
    const requestedPoolSignature = poolSignature(requestedPool);
    const generationFlowId = newScheduleFlowId();
    const generationKind = whatIf ? "what_if" : flow;
    const generationStartedAt = Date.now();
    let generationTerminalSent = false;
    const finishGeneration = (outcome: "success" | "error" | "cancelled" | "incomplete", returnedCount: number) => {
      if (generationTerminalSent) return;
      generationTerminalSent = true;
      if (outcome !== "cancelled") scheduleStepCompleted("timetable", outcome, scheduleFlowIdRef.current);
      scheduleGenerationTerminal({
        flowId: generationFlowId,
        flow: generationKind,
        outcome,
        durationMs: Date.now() - generationStartedAt,
        requestedCount: requestedPool.length,
        returnedCount,
      });
    };
    const isCurrentRequest = () => (
      termRef.current === requestedTerm
      && generationEpochRef.current === requestedEpoch
      && poolSignature(poolRef.current) === requestedPoolSignature
    );
    setGenerateProgress({ done: 0, total: catalogCourses.length });
    try {
      const unavailable: string[] = [];
      const unpublished: string[] = [];
      // One request for everything still missing, rather than one per course
      // inside the loop below. A pool the catalog has already seen this week
      // costs a single call that touches no campus page at all.
      let known = sectionsByCourse;
      try {
        known = await fetchPoolSections(requestedPool, known);
      } catch (error) {
        // Not fatal: the per-course path below still runs, so a failed batch
        // costs speed rather than the whole attempt.
        captureRequestFailure(error, { operation: "schedule.pool_sections", kind: "query" });
      }
      if (!isCurrentRequest()) { finishGeneration("cancelled", 0); return false; }
      for (const [index, course] of requestedPool.entries()) {
        if (!isCurrentRequest()) { finishGeneration("cancelled", 0); return false; }
        setGenerateProgress({ done: index + 1, total: requestedPool.length });
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
      if (!isCurrentRequest()) { finishGeneration("cancelled", 0); return false; }
      setSectionsByCourse((current) => {
        const active = Object.fromEntries(Object.entries(known).filter(([identity]) => poolRef.current.some((course) => courseIdentity(course.rawCode) === identity)));
        return { ...current, ...active };
      });
      const freshlyFetchedConstraints = await fetchAllConstraintsRef.current(requestedPool);
      if (!isCurrentRequest()) return false;
      const currentConstraints = { ...constraints, ...freshlyFetchedConstraints };
      const regenerated = await submitPlanningUpdate({
        // Mode, pool, locks, personal blocks and the solver result are one
        // owner revision. A late catalog answer or a failed solve can no
        // longer leave a half-staged pool behind.
        operation: "regenerate",
        what_if: whatIf,
        empty_days: emptyDays,
        avoid_conflicts: avoidConflicts,
        locked_sections: lockedSections,
        pool: requestedPool.map((course) => ({
          code: course.code,
          name: course.name,
          credits: course.credits,
          raw_code: course.rawCode,
          required: course.required !== false,
        })),
        sections: canonicalSectionsFromLocal(known, currentConstraints),
      });
      if (!isCurrentRequest()) { finishGeneration("cancelled", 0); return false; }
      if (!regenerated) {
        setUnscheduledCourses(requestedPool.map((course) => course.code));
        setGenerationError(t("Dersler doğrulanamadı; program kaydedilmedi. Ayrıntıları kontrol edip tekrar dene.", "The courses could not be verified, so the schedule was not saved. Review the details and try again."));
        generationKeyRef.current = autoGenerationKey;
        finishGeneration("error", 0);
        return true;
      }
      const scheduled = new Set(regenerated.state.entries.filter((entry) => entry.kind === "course").map((entry) => courseIdentity(entry.code)));
      const sectionPayload = canonicalSectionsFromLocal(known, currentConstraints);
      const restricted = requestedPool
        .filter((course) => {
          const rows = sectionPayload[courseIdentity(course.rawCode)] ?? [];
          return rows.length > 0 && rows.every((row) => row && typeof row === "object" && (row as { eligible?: boolean }).eligible === false);
        })
        .map((course) => course.code);
      const unplaced = requestedPool
        .filter((course) => !scheduled.has(courseIdentity(course.code)))
        .filter((course) => !unavailable.includes(course.code) && !unpublished.includes(course.code) && !restricted.includes(course.code))
        .map((course) => course.code);
      const unscheduled = regenerated.state.unscheduled_courses?.length
        ? regenerated.state.unscheduled_courses
        : [...new Set([...unavailable, ...unpublished, ...restricted, ...unplaced])];

      if (unavailable.length) toast.error(t(`${unavailable.join(", ")} için ODTÜ sisteminde şube bulunamadı.`, `No sections were found in METU's system for ${unavailable.join(", ")}.`));
      if (unpublished.length) toast.warning(t(`${unpublished.join(", ")} için gün ve saat ODTÜ tarafından henüz yayımlanmadı.`, `METU has not published days and times for ${unpublished.join(", ")} yet.`));
      if (restricted.length) toast.warning(t(`${restricted.join(", ")} için soyadına açık şube yok. Kısıtları yok sayarak tekrar dene.`, `No section of ${restricted.join(", ")} is open to your surname. Try again with restrictions ignored.`));
      if (unplaced.length) toast.warning(t(`${unplaced.join(", ")} için boş gün ve çakışma tercihlerine uyan şube yok.`, `No section of ${unplaced.join(", ")} fits your empty-day and conflict preferences.`));
      if (unscheduled.length && regenerated.state.generation_error && !unavailable.length && !unpublished.length && !restricted.length && !unplaced.length) {
        toast.warning(t("Tüm seçili dersler için eksiksiz bir program oluşturulamadı. Programı ve eksik dersleri gözden geçir.", "A complete schedule could not be generated for every selected course. Review the timetable and unscheduled courses."));
      }
      if (regenerated.state.alternatives.length > 1) {
        toast.success(t(
          `${regenerated.state.alternatives.length} alternatif program bulundu. Oklarla aralarında geçiş yap.`,
          `${regenerated.state.alternatives.length} possible schedules found. Use the arrows to switch between them.`,
        ));
      } else if (regenerated.state.alternatives.length === 1 && !unavailable.length && !unpublished.length && !unplaced.length) {
        toast.success(t("Tek bir çakışmasız program mümkün.", "Exactly one conflict-free schedule is possible."));
      }
      generationKeyRef.current = autoGenerationKey;
      finishGeneration(unscheduled.length ? "incomplete" : "success", scheduled.size);
      return true;
    } catch (error) {
      if (!isCurrentRequest()) { finishGeneration("cancelled", 0); return false; }
      toast.error(error instanceof Error ? error.message : t("Program oluşturulamadı.", "The schedule could not be generated."));
      setUnscheduledCourses(requestedPool.map((course) => course.code));
      setGenerationError(error instanceof Error ? error.message : t("Program oluşturulamadı.", "The schedule could not be generated."));
      generationKeyRef.current = autoGenerationKey;
      finishGeneration("error", 0);
      return true;
    } finally { setGenerateProgress(null); }
  }, [autoGenerationKey, avoidConflicts, catalogCourses, constraints, emptyDays, fetchPoolSections, fetchSections, lockedSections, planning.conflict, planning.ready, planning.retryable, planning.saving, sectionsByCourse, submitPlanningUpdate, t, term, whatIf]);

  useEffect(() => {
    if (!hydrated || activeStep !== "timetable" || !catalogCourses.length || busy) return;
    if (generationKeyRef.current === autoGenerationKey) return;
    const timer = window.setTimeout(() => {
      if (generationInFlightRef.current) return;
      generationInFlightRef.current = true;
      void generateSchedule().finally(() => { generationInFlightRef.current = false; });
    }, 650);
    return () => window.clearTimeout(timer);
  }, [activeStep, autoGenerationKey, busy, catalogCourses.length, generateSchedule, hydrated]);

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
    invalidateGenerationRequest();
    setEntries(alternatives[next]);
  }

  function addCatalogSection(course: CatalogCourse, section: CatalogSection) {
    if (!section.meetings.length) return toast.warning(t("Bu şubenin gün ve saati ODTÜ tarafından henüz yayımlanmamış.", "METU has not published this section's day and time yet."));
    const check = sectionAllowed(course, section);
    if (!check.allowed && !whatIf) {
      return toast.error(check.reason
        ? t(`Bu şube eklenemedi: ${localizedRestrictionReason(check.reason, t)}.`, `This section cannot be added: ${localizedRestrictionReason(check.reason, t)}.`)
        : t("Bu şubenin uygunluğu doğrulanmadı. Keşif modunu açarak deneyebilirsin.", "This section is not verified. Turn on what-if mode to explore it."));
    }
    // Eligibility is checked by the canonical owner when this state is saved.
    // The browser may show the catalog verdict, but it cannot make the
    // registration decision that chat and other clients must also observe.
    const additions = section.meetings.map((meeting, index) => ({ id: crypto.randomUUID(), code: course.code, name: course.name, section: section.section, credits: index === 0 ? course.credits : 0, color: uniqueCourses % COLORS.length, kind: "course" as const, instructor: section.instructor, ...meeting }));
    if (avoidConflicts && additions.some((next) => entries.some((entry) => overlaps(entry, next)))) return toast.error(t("Bu şube mevcut programla çakışıyor.", "This section conflicts with your schedule."));
    invalidateGenerationRequest();
    setEntries((current) => [...current, ...additions]);
    if (!check.allowed) toast.warning(t("Şube keşif modunda eklendi; kayıt uygunluğunu ODTÜ'de doğrula.", "Section added in what-if mode; verify registration eligibility with METU."));
    toast.success(t(`${course.code} şube ${section.section} eklendi.`, `${course.code} section ${section.section} added.`));
  }

  function removeSelectedEntry() {
    if (!selectedEntry) return;
    if (selectedEntry.kind === "block") {
      invalidateGenerationRequest();
      setEntries((current) => current.filter((entry) => entry.id !== selectedEntry.id));
    } else {
      const code = courseIdentity(selectedEntry.code);
      const course = catalogCourses.find((candidate) => courseIdentity(candidate.code) === code || courseIdentity(candidate.rawCode) === code);
      if (course) removePoolCourse(course);
      else {
        invalidateGenerationRequest();
        setEntries((current) => current.filter((entry) => entry.kind !== "course" || courseIdentity(entry.code) !== code || entry.section !== selectedEntry.section));
      }
    }
    setSelectedEntry(null);
  }

  function replaceSelectedCourseSection(course: CatalogCourse, section: CatalogSection) {
    if (!selectedEntry || selectedEntry.kind !== "course") return;
    const check = sectionAllowed(course, section);
    if (!section.meetings.length || (!check.allowed && !whatIf)) {
      toast.error(check.reason
        ? t(`Bu şube seçilemedi: ${localizedRestrictionReason(check.reason, t)}.`, `This section cannot be selected: ${localizedRestrictionReason(check.reason, t)}.`)
        : t("Bu şubenin uygunluğu doğrulanmadı.", "This section is not verified."));
      return;
    }
    const courseKey = courseIdentity(selectedEntry.code);
    const remaining = entries.filter((entry) => entry.kind !== "course" || courseIdentity(entry.code) !== courseKey);
    const additions = section.meetings.map((meeting, index) => ({
      id: crypto.randomUUID(),
      code: course.code,
      name: course.name,
      section: section.section,
      credits: index === 0 ? course.credits : 0,
      color: selectedEntry.color,
      kind: "course" as const,
      instructor: section.instructor,
      ...meeting,
    }));
    if (avoidConflicts && additions.some((next) => remaining.some((entry) => overlaps(entry, next)))) {
      toast.error(t("Bu şube mevcut programla çakışıyor.", "This section conflicts with your schedule."));
      return;
    }
    invalidateGenerationRequest();
    setEntries([...remaining, ...additions]);
    setSelectedEntry(additions[0] ?? null);
    toast.success(t(`${course.code} şube ${section.section} seçildi.`, `${course.code} section ${section.section} selected.`));
  }

  function toggleSectionLock(course: CatalogCourse, section: CatalogSection) {
    const key = courseIdentity(course.rawCode);
    touchGenerationInputs();
    setLockedSections((current) => {
      if (current[key] === section.section) {
        const next = { ...current };
        delete next[key];
        return next;
      }
      return { ...current, [key]: section.section };
    });
    toast.success(t(
      lockedSections[key] === section.section ? `${course.code} şube ${section.section} kilidi kaldırıldı.` : `${course.code} şube ${section.section} kilitlendi.`,
      lockedSections[key] === section.section ? `${course.code} section ${section.section} unlocked.` : `${course.code} section ${section.section} locked.`,
    ));
  }

  // --- sharing and export -------------------------------------------------

  function favorite() {
    if (!entries.length) return;
    const next = [...favorites, entries].slice(-10);
    setFavorites(next); setFavoriteIndex(next.length - 1);
    invalidateGenerationRequest();
    toast.success(t("Program favorilere eklendi.", "Schedule added to favorites."));
  }

  function nextFavorite() {
    if (!favorites.length) return;
    // Tracked by index rather than by comparing the current entries against
    // each favorite: entries are replaced wholesale by every other action, so
    // an identity comparison never matched and this always showed the first.
    const index = (favoriteIndex + 1) % favorites.length;
    setFavoriteIndex(index);
    invalidateGenerationRequest();
    setEntries(favorites[index] ?? []);
  }

  function clearSchedule() {
    invalidateGenerationRequest();
    setEntries([]);
    setSelectedEntry(null);
    setUnscheduledCourses([]);
    setGenerationError("");
  }

  async function copySummary() {
    const summary = DAYS.map((day) => `${dayLabel(day)}: ${entries.filter((e) => e.day === day).sort((a, b) => itemStartMinute(a) - itemStartMinute(b)).map((e) => `${formatItemTime(e)} ${e.code}-${e.section} (${e.kind === "course" ? (e.room?.trim() || "TBA") : (e.room?.trim() || "—")})`).join(", ") || "—"}`).join("\n");
    await navigator.clipboard.writeText(summary); toast.success(t("Program özeti kopyalandı.", "Schedule summary copied."));
  }

  function exportCsv() {
    const flowId = newScheduleFlowId();
    if (!entries.length) {
      scheduleExportTerminal({ flowId, format: "csv", outcome: "incomplete", status: null });
      return toast.error(t("Dışa aktarılacak ders yok.", "There is nothing to export yet."));
    }
    try {
      const header = [t("Dönem modu", "Plan mode"), t("Gün", "Day"), t("Başlangıç", "Start"), t("Bitiş", "End"), t("Kod", "Code"), t("Ders adı", "Course name"), t("Şube", "Section"), t("Öğretim elemanı", "Instructor"), t("Derslik", "Room"), t("Kredi", "Credits")];
      const rows = [...entries]
        .sort((a, b) => DAYS.indexOf(a.day) - DAYS.indexOf(b.day) || itemStartMinute(a) - itemStartMinute(b))
        .map((entry) => [whatIf ? t("Keşif", "What-if") : t("Normal", "Normal"), dayLabel(entry.day), formatItemTime(entry), formatClock(itemStartMinute(entry) + itemDurationMinutes(entry)), entry.code, entry.name, entry.section, entry.instructor ?? "", entry.kind === "course" ? (entry.room?.trim() || "TBA") : entry.room, entry.credits]);
      const csv = [header, ...rows].map((row) => row.map(csvCell).join(";")).join("\r\n");
      downloadFile(`devrimo-${term}.csv`, new Blob([`﻿${csv}`], { type: "text/csv;charset=utf-8" }));
      scheduleExportTerminal({ flowId, format: "csv", outcome: "success", status: null });
      toast.success(t("Program CSV olarak indirildi.", "Schedule downloaded as CSV."));
    } catch (error) {
      scheduleExportTerminal({ flowId, format: "csv", outcome: "error", status: null });
      captureRequestFailure(error, { operation: "schedule.export_csv", kind: "mutation" });
      toast.error(error instanceof Error ? error.message : t("CSV dışa aktarılamadı.", "The CSV could not be exported."));
    }
  }

  function exportWallpaper() {
    const flowId = newScheduleFlowId();
    if (!entries.length) {
      scheduleExportTerminal({ flowId, format: "wallpaper", outcome: "incomplete", status: null });
      return toast.error(t("Dışa aktarılacak ders yok.", "There is nothing to export yet."));
    }
    try {
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
      const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><rect width="100%" height="100%" fill="#171312"/><text x="180" y="170" fill="white" font-size="72" font-family="Arial" font-weight="700">Devrimo · ${termLabel(term, t).replace(/[<>&]/g, "")}${whatIf ? t(" · Keşif modu", " · What-if").replace(/[<>&]/g, "") : ""}</text>${DAYS.map((d, i) => `<text x="${500 + i * 650}" y="285" fill="#aaa" font-size="36" font-family="Arial">${dayLabel(d)}</text>`).join("")}${cells}</svg>`;
      downloadFile("devrimo-schedule-4k.svg", new Blob([svg], { type: "image/svg+xml" }));
      scheduleExportTerminal({ flowId, format: "wallpaper", outcome: "success", status: null });
    } catch (error) {
      scheduleExportTerminal({ flowId, format: "wallpaper", outcome: "error", status: null });
      captureRequestFailure(error, { operation: "schedule.export_wallpaper", kind: "mutation" });
      toast.error(error instanceof Error ? error.message : t("Duvar kâğıdı dışa aktarılamadı.", "The wallpaper could not be exported."));
    }
  }

  async function exportIcs() {
    const flowId = newScheduleFlowId();
    if (!entries.length) {
      scheduleExportTerminal({ flowId, format: "ics", outcome: "incomplete", status: null });
      return toast.error(t("Dışa aktarılacak ders yok.", "There is nothing to export yet."));
    }
    if (!semesterStart || !semesterEnd || semesterEnd <= semesterStart) {
      scheduleExportTerminal({ flowId, format: "ics", outcome: "incomplete", status: null });
      return toast.error(t("Geçerli dönem başlangıç ve bitiş tarihlerini seç.", "Choose valid semester start and end dates."));
    }
    if (!semesterDatesConfirmed) {
      scheduleExportTerminal({ flowId, format: "ics", outcome: "incomplete", status: null });
      return toast.error(t("Takvim tarihlerini doğruladığını onayla.", "Confirm that the calendar dates match your term dates."));
    }
    try {
      const query = new URLSearchParams({ term, start_date: semesterStart, end_date: semesterEnd, revision: String(planning.revision) });
      const requestId = newRequestId();
      const response = await fetch(`/api/schedule/timetable/export.ics?${query.toString()}`, { credentials: "include", headers: { [REQUEST_ID_HEADER]: requestId } });
      if (!response.ok) throw await apiErrorFromResponse(response, requestId);
      downloadFile(`devrimo-${term}.ics`, await response.blob());
      scheduleExportTerminal({ flowId, format: "ics", outcome: "success", status: response.status, requestId: response.headers.get(REQUEST_ID_HEADER) ?? requestId });
      toast.success(t("Takvim dosyası indirildi.", "Calendar file downloaded."));
    } catch (error) {
      scheduleExportTerminal({ flowId, format: "ics", outcome: "error", status: error instanceof ApiError ? error.status : null, requestId: requestIdOf(error) });
      captureRequestFailure(error, { operation: "schedule.export_ics", kind: "mutation" });
      toast.error(error instanceof Error ? error.message : t("Takvim dışa aktarılamadı.", "The calendar could not be exported."));
    }
  }

  // --- render -------------------------------------------------------------
  // Below xl the page scrolls as one column. From xl up it is pinned to the
  // viewport: the sidebar and the timetable each scroll inside themselves, so
  // the week is always fully visible without moving the page.

  return (
    <div className="ph-no-capture h-full overflow-y-auto bg-[radial-gradient(circle_at_85%_0%,rgb(227_24_55/8%),transparent_32%)] px-4 py-4 sm:px-6 lg:px-8 xl:flex xl:flex-col xl:overflow-y-auto" data-ph-no-autocapture>
      <AlertDialog open={prerequisiteRejections.length > 0} onOpenChange={(open) => { if (!open) setPrerequisiteRejections([]); }}>
        <AlertDialogContent className="ph-no-capture" data-ph-no-autocapture>
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
            <label className="flex items-center gap-2 rounded-md border bg-background px-2 text-xs text-muted-foreground">
              <span>{t("Dönem", "Term")}</span>
              <select
                value={term}
                onChange={(event) => {
                  const next = event.target.value;
                  if (!publishedTerms.includes(next)) return;
                  setTerm(next);
                  if (typeof window !== "undefined") {
                    const url = new URL(window.location.href);
                    url.searchParams.set("term", next);
                    window.history.pushState({}, "", url);
                  }
                }}
                className="h-8 max-w-[170px] bg-transparent font-medium text-foreground outline-none"
                aria-label={t("Dönem seç", "Select term")}
                disabled={publishedTermsLoading || publishedTerms.length === 0}
              >
                {publishedTerms.length
                  ? publishedTerms.map((value) => <option key={value} value={value}>{termLabel(value, t)}</option>)
                  : <option value={term}>{publishedTermsLoading ? t("Dönemler yükleniyor…", "Loading terms…") : t("Dönem bulunamadı", "No published term")}</option>}
              </select>
            </label>
            <Button variant="outline" size="sm" onClick={clearSchedule}><RotateCcwIcon />{t("Programı temizle", "Clear schedule")}</Button>
            <Button variant="outline" size="sm" onClick={() => void handleUndo()} disabled={!planning.envelope?.can_undo || planning.saving || planning.retryable || Boolean(planning.conflict)}><RotateCcwIcon />{t("Geri al", "Undo")}</Button>
          </div>
        </div>

        {publishedTermsLoading ? <p className="rounded-lg border bg-muted/30 p-2 text-xs leading-5 text-muted-foreground" role="status">{t("Planlanabilir dönemler yükleniyor…", "Loading published planning terms…")}</p> : publishedTermsError ? <p className="rounded-lg border border-destructive/30 bg-destructive/5 p-2 text-xs leading-5 text-destructive" role="alert">{t("Planlanabilir dönemler alınamadı; yeniden dene.", "Published planning terms could not be loaded; try again.")}</p> : !termIsPublished ? <p className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-2 text-xs leading-5 text-amber-800 dark:text-amber-200" role="status">{t("Bu dönem katalogda yayınlanmadı.", "This term is not published in the catalog.")}</p> : null}

        {/* Above both columns: it explains the whole screen, and once dismissed
            it shrinks to a single link rather than taking space forever. */}
        {activeStep !== "details" ? <PlannerIntro key={activeStep} activeStep={activeStep} className="sm:mb-4" /> : null}

        <PlannerStepNav active={activeStep} completed={completed} canOpen={canOpen} onSelect={navigateStep} t={t} />

        {activeStep === "details" ? (
          <Card className="mx-auto w-full max-w-3xl">
            <CardHeader>
              <CardTitle>{t("Önce seni doğrulayalım", "First, verify your details")}</CardTitle>
              <p className="text-sm leading-6 text-muted-foreground">{t("Programı gerçek ders uygunluğuna göre kurabilmemiz için hesabını ve ODTÜ'den gelen akademik bağlamını doğrula.", "We need your account and the academic context returned by METU before we can build a registration-ready schedule.")}</p>
            </CardHeader>
            <CardContent className="space-y-4">
              <section className="rounded-xl border bg-background/60 p-4" aria-labelledby="planner-account-heading">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 id="planner-account-heading" className="font-semibold">{t("Hesap ve ODTÜ bağlantısı", "Account and METU connection")}</h2>
                    <p className="mt-1 text-xs leading-5 text-muted-foreground">{t("Ders, transkript ve şube uygunluğu yalnızca doğrulanmış SAIS bağlantısıyla okunur.", "Courses, transcript data, and section eligibility are read only through a verified SAIS connection.")}</p>
                  </div>
                  <span className={cn("rounded-full px-2.5 py-1 text-xs font-medium", verifiedCampus ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" : "bg-amber-500/10 text-amber-700 dark:text-amber-300")}>
                    {verifiedCampus ? t("Doğrulandı", "Verified") : t("Gerekli", "Required")}
                  </span>
                </div>
                <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
                  <span data-ph-mask>{connection?.metu_username || t("Henüz bağlantı yok", "No connection yet")}</span>
                  <a href="/settings" className="font-medium text-primary underline-offset-4 hover:underline">
                    {verifiedCampus ? t("Bağlantıyı yönet", "Manage connection") : t("Kimlik bilgilerini ekle", "Add credentials")}
                  </a>
                </div>
                {!verifiedCampus ? <p className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-xs leading-5">{t("Ayarlar'da ODTÜ kullanıcı adını ve parolanı ekleyip doğrulamayı tamamla. Parolanı burada göstermiyoruz.", "Add your METU username and password in Settings, then complete verification. Your password is never shown here.")}</p> : null}
              </section>

              <section className="rounded-xl border bg-background/60 p-4" aria-labelledby="planner-academic-heading" data-ph-mask>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 id="planner-academic-heading" className="font-semibold">{t("Akademik bağlam", "Academic context")}</h2>
                    <p className="mt-1 text-xs leading-5 text-muted-foreground">{t("Bölüm, program ve sınıf bilgisi SAIS'ten gelir. Soyadının ilk iki harfi, sınıf kısıtlarını doğru göstermek için kullanılır.", "Department, programme, and year come from SAIS. The first two letters of your surname help us evaluate section restrictions.")}</p>
                  </div>
                  {contextLoading ? <Loader2Icon className="size-4 animate-spin text-muted-foreground" aria-label={t("Yükleniyor", "Loading")} /> : <span className={cn("rounded-full px-2.5 py-1 text-xs font-medium", verifiedAcademicContext ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" : "bg-amber-500/10 text-amber-700 dark:text-amber-300")}>{verifiedAcademicContext ? t("SAIS doğrulandı", "SAIS verified") : t("Bekleniyor", "Waiting")}</span>}
                </div>
                {contextError ? <p className="mt-3 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-xs leading-5 text-destructive" role="alert">{contextError}</p> : null}
                <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
                  <div className="col-span-2"><dt className="text-[11px] uppercase tracking-wide text-muted-foreground">{t("Bölüm", "Department")}</dt><dd className="text-sm font-medium" data-ph-mask>{studentContext?.department || "—"}</dd></div>
                  <div><dt className="text-[11px] uppercase tracking-wide text-muted-foreground">{t("Program", "Programme")}</dt><dd className="text-sm font-medium" data-ph-mask>{studentContext?.program_code || "—"}</dd></div>
                  <div><dt className="text-[11px] uppercase tracking-wide text-muted-foreground">{t("Sınıf", "Year")}</dt><dd className="text-sm font-medium" data-ph-mask>{studentContext?.year_of_study || "—"}</dd></div>
                </dl>
                <div className="mt-4 grid gap-3 sm:grid-cols-2">
                  <Field id="planner-program" label={t("Program kodu", "Programme code")}>
                    <Input id="planner-program" data-ph-mask value={programDraft ?? ""} onChange={(event) => setProgramDraft(event.target.value.slice(0, 32))} placeholder={t("Örn. CENG", "e.g. CENG")} />
                  </Field>
                  <Field id="planner-year" label={t("Sınıf", "Year of study")}>
                    <select id="planner-year" data-ph-mask value={yearDraft ?? ""} onChange={(event) => setYearDraft(event.target.value ? Number(event.target.value) : null)} className="h-10 w-full rounded-md border bg-background px-2 text-sm">
                      <option value="">{t("Belirtilmedi", "Not provided")}</option>
                      {[1, 2, 3, 4, 5, 6, 7, 8, 9].map((year) => <option key={year} value={year}>{year}. {t("sınıf", "year")}</option>)}
                    </select>
                  </Field>
                </div>
                <div className="mt-4 grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-end">
                  <Field id="planner-surname" label={t("Soyadının ilk iki harfi", "First two letters of your surname")}>
                    <Input id="planner-surname" data-ph-mask value={surnameDraft ?? ""} onChange={(event) => setSurnameDraft(event.target.value.toLocaleUpperCase("tr-TR").replace(/[^A-ZÇĞİÖŞÜ]/gi, "").slice(0, 2))} maxLength={2} autoComplete="family-name" placeholder="AB" />
                  </Field>
                  <Button variant="outline" onClick={() => void saveStudentContext()} disabled={contextSaving || contextLoading || !contextDraftValid}>{contextSaving ? <Loader2Icon className="animate-spin" /> : null}{t("Kaydet", "Save")}</Button>
                </div>
                <p className="text-[11px] leading-5 text-muted-foreground">{t("Soyadının ilk iki harfini girmen gerekir.", "Enter the first two letters of your surname.")}</p>
                {verifiedCampus && !verifiedAcademicContext ? <div className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3">
                  <p className="text-xs leading-5">{t("Elle kaydedilen bilgiler SAIS doğrulamasını kaldırdı. Güncel akademik bağlamı yeniden getir.", "Saving manual details clears SAIS verification. Refresh your academic context to verify it again.")}</p>
                  <Button size="sm" variant="outline" onClick={() => void refreshStudentContext()} disabled={contextRefreshing || contextLoading}>{contextRefreshing ? <Loader2Icon className="animate-spin" /> : null}{t("SAIS'ten yenile", "Refresh from SAIS")}</Button>
                </div> : null}
              </section>

              <p className="rounded-xl border bg-muted/25 p-3 text-xs leading-5 text-muted-foreground">{t("Güvenliğin için ODTÜ kimlik bilgilerin şifreli tutulur. Akademik bağlam ve transkript ders kodları planlama için saklanır; ders adları, kodları veya notların analitik olaylara gönderilmez. Ayarlar'dan akademik verileri sildiğinde kayıtlı programların ve geçmişlerinin de silineceği açıklanır.", "Your METU credentials are encrypted. Academic context and transcript course codes are retained for planning; course names, codes, and grades are not sent as analytics events. Settings explains that deleting academic data also removes saved schedules and their history.")}</p>

              <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
                <Button variant="ghost" onClick={() => { touchGenerationInputs(); setWhatIf(true); setIgnoreConstraints(true); navigateStep("courses"); }} disabled={contextLoading || !detailsReady}>{t("Keşif modunda devam et", "Continue in what-if mode")}</Button>
                <Button onClick={() => navigateStep("courses")} disabled={!detailsReady}>{t("Derslere geç", "Continue to courses")}{!detailsReady ? <span className="text-xs opacity-70">({t("doğrulama gerekli", "verification required")})</span> : null}</Button>
              </div>
            </CardContent>
          </Card>
        ) : null}

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

        <div className={cn("grid gap-4 xl:min-h-0 xl:flex-1 xl:grid-cols-[340px_minmax(0,1fr)]", activeStep === "details" && "hidden")}>
          <aside className="min-w-0 space-y-4 xl:min-h-0 xl:overflow-y-auto xl:pr-1">
            <Card><CardContent className="grid grid-cols-[minmax(0,1fr)] gap-3 p-4">
              {/* The department is not a setting. It is read once from SAIS and
                  the broker keeps it, so in the normal case there is nothing
                  here to show or decide — this whole block appears only when
                  the campus systems gave us nothing to go on. */}
              {departmentBusy || departmentKnown ? null : (
                <Field id="planner-department" label={t("Bölüm", "Department")}>
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
                      <Input id="planner-department" value={departmentQuery} onChange={(e) => setDepartmentQuery(e.target.value)} className="pl-9" placeholder={t("Bölüm ara (ör. Bilgisayar)", "Search department (e.g. Computer)")} aria-label={t("Bölüm ara", "Search department")} />
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
                        onClick={() => { touchGenerationInputs(); setEmptyDays((current) => chosen ? current.filter((item) => item !== day) : [...current, day]); }}
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
              <Toggle label={t("Çakışmaları engelle", "Prevent conflicts")} checked={avoidConflicts} onChange={(value) => { touchGenerationInputs(); setAvoidConflicts(value); }} />
              <span data-tour="rules">
                <Toggle label={t("Keşif modu", "What-if mode")} checked={whatIf} onChange={(checked) => { touchGenerationInputs(); setWhatIf(checked); setIgnoreConstraints(checked); }} />
                <span className="mt-1 block text-[11px] leading-5 text-muted-foreground">{whatIf ? t("Doğrulanmamış veya kapalı şubeleri deneyebilirsin; bu program kayıt garantisi vermez.", "You can explore unverified or closed sections; this schedule is not a registration guarantee.") : t("Yalnızca SAIS bağlamınla doğrulanmış şubeler programa alınır.", "Only sections verified against your SAIS context are scheduled.")}</span>
              </span>
            </CardContent></Card>

            <Card data-tour="pool" data-ph-mask><CardHeader className="pb-3"><CardTitle className="text-base">{t("Dönem dersleri", "Semester courses")}</CardTitle></CardHeader><CardContent className="grid grid-cols-[minmax(0,1fr)] gap-3">
              <Button onClick={() => void loadRequiredCourses()} disabled={busy || departmentBusy}>{planBusy ? t("Dersler belirleniyor…", "Finding courses…") : t("Almam gereken dersleri getir", "Load required courses")}</Button>
              <Button variant="outline" onClick={() => void loadFullCurriculum()} disabled={fullCurriculumBusy || departmentBusy}>{fullCurriculumBusy ? <Loader2Icon className="animate-spin" /> : null}{t("Tam müfredatı gör", "View full curriculum")}</Button>
              {planBusy ? <p className="flex items-center gap-2 rounded-lg border bg-muted/30 p-2 text-xs text-muted-foreground" role="status" aria-live="polite"><Loader2Icon className="size-3.5 animate-spin" />{t("Müfredatın okunuyor…", "Reading your curriculum…")}</p> : null}
              {curriculumNotice ? <p className="rounded-lg border bg-muted/30 p-2 text-xs leading-5 text-muted-foreground">{curriculumNotice}</p> : null}
              {catalogCourses.length ? (
                <div className="flex items-center justify-between gap-3 text-xs" aria-live="polite">
                  <span className="font-medium">{t(`${catalogCourses.length} dersten ${selectedPoolCount} tanesi programa eklendi`, `${selectedPoolCount} of ${catalogCourses.length} courses added to the schedule`)}</span>
                  <span className="shrink-0 tabular-nums text-muted-foreground">{selectedPoolCount}/{catalogCourses.length}</span>
                </div>
              ) : null}
              {constraintsBusy ? <p className="flex items-center gap-2 rounded-lg border bg-muted/30 p-2 text-xs leading-5 text-muted-foreground"><Loader2Icon className="size-3.5 shrink-0 animate-spin" />{t("Şube kısıtları ODTÜ'den okunuyor; kırmızı işaretler geldikçe belirecek.", "Reading section restrictions from METU; red flags will appear as they arrive.")}</p> : null}
              {fullCurriculumWarning ? <p className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-2 text-xs leading-5 text-amber-800 dark:text-amber-200" role="status">{fullCurriculumWarning}</p> : null}
              {fullCurriculum.length ? <div className="space-y-2 rounded-lg border bg-background/60 p-3" data-ph-mask>
                {academicSnapshot ? <p className="text-xs leading-5 text-muted-foreground">{t(`SAIS kaydında ${academicSnapshot.term} döneminde ${academicSnapshot.enrolled_course_count} kayıtlı ders görünüyor.`, `The SAIS snapshot for ${academicSnapshot.term} shows ${academicSnapshot.enrolled_course_count} currently enrolled courses.`)}</p> : null}
                <div className="grid grid-cols-3 gap-2 text-[11px]" aria-label={t("Müfredat özeti", "Curriculum summary")}>
                  <span className="rounded-md bg-emerald-500/10 px-2 py-1 text-emerald-700 dark:text-emerald-300">{curriculumSummary.completed} {t("tamamlandı", "completed")}</span>
                  <span className="rounded-md bg-destructive/10 px-2 py-1 text-destructive">{curriculumSummary.failed} {t("tekrar", "repeat")}</span>
                  <span className="rounded-md bg-muted px-2 py-1 text-muted-foreground">{curriculumSummary.outstanding} {t("bekliyor", "outstanding")}</span>
                </div>
                <div className="max-h-72 space-y-3 overflow-y-auto pr-1">
                {[...new Set(fullCurriculum.map((course) => course.semester))].sort((a, b) => a - b).map((semester) => (
                  <section key={semester} aria-labelledby={`curriculum-semester-${semester}`}>
                    <h3 id={`curriculum-semester-${semester}`} className="mb-1 text-xs font-semibold text-muted-foreground">{t(`Dönem ${semester}`, `Semester ${semester}`)}{fullCurriculum.some((course) => course.semester === semester && course.semester_completed) ? ` · ${t("tamamlandı", "completed")}` : ` · ${t("açık", "open")}`}</h3>
                    <ul className="space-y-1">
                      {fullCurriculum.filter((course) => course.semester === semester).map((course) => <li key={`${course.semester}-${course.course_code}`} className="flex items-center gap-2 text-xs"><span className="font-mono font-semibold">{formatMetuCourseCode(course.course_code)}</span><span className="min-w-0 flex-1 truncate">{localizedCourseName(course.course_name, locale)}</span><span className="shrink-0 text-[11px] text-muted-foreground">{course.credits ? `${course.credits} ${t("kr", "cr")}` : ""}</span><span className="shrink-0 rounded-full bg-muted px-1.5 py-0.5 text-muted-foreground">{course.grade || (course.status === "outstanding" ? t("Not yok", "No grade") : "—")}</span><span className={cn("shrink-0 rounded-full px-1.5 py-0.5", course.status === "completed" ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" : course.status === "failed" ? "bg-destructive/10 text-destructive" : "bg-muted text-muted-foreground")}>{course.status === "completed" ? t("Geçti", "Passed") : course.status === "failed" ? t("Tekrar", "Repeat") : t("Bekliyor", "Outstanding")}</span></li>)}
                    </ul>
                  </section>
                ))}
                </div>
              </div> : null}
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
                                <div
                                  key={section.section}
                                  role="button"
                                  tabIndex={check.allowed || whatIf ? 0 : -1}
                                  onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); addCatalogSection(course, section); } }}
                                  onClick={() => addCatalogSection(course, section)}
                                  className={cn("w-full rounded-lg border bg-background p-2 text-left text-sm transition hover:border-primary/40 hover:bg-primary/5", !eligible && !whatIf && "opacity-60")}
                                >
                                  <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
                                    <span className="font-semibold">{t("Şube", "Section")} {section.section}</span>
                                    <span className={cn("rounded-full px-1.5 py-0.5 text-[10px] font-medium", check.status === "verified" && eligible ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" : "bg-amber-500/10 text-amber-700 dark:text-amber-300")}>{check.status === "verified" && eligible ? t("Uygun", "Eligible") : check.status === "unavailable" ? t("Doğrulanamadı", "Unavailable") : t("Bekliyor", "Unverified")}</span>
                                    <Button type="button" size="icon-sm" variant="ghost" className="ml-auto" aria-label={lockedSections[courseIdentity(course.rawCode)] === section.section ? t("Kilidi kaldır", "Unlock section") : t("Şubeyi kilitle", "Lock section")} onClick={(event) => { event.stopPropagation(); toggleSectionLock(course, section); }}><span aria-hidden="true">{lockedSections[courseIdentity(course.rawCode)] === section.section ? "🔒" : "🔓"}</span></Button>
                                  </span>
                                  {!eligible ? <span className="mt-1 flex items-start gap-1.5 rounded-md bg-destructive/10 px-2 py-1.5 text-xs leading-snug text-destructive"><TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />{closedLabel}</span> : null}
                                  {section.instructor ? <span className="mt-0.5 block break-words text-xs font-medium text-foreground/80">{section.instructor}</span> : null}
                                  <span className="mt-1 block break-words text-xs text-muted-foreground">{section.meetings.length ? section.meetings.map((meeting) => `${dayLabel(meeting.day)} ${formatItemRange(meeting)} · ${meeting.room?.trim() || "TBA"}`).join(" / ") : t("Gün ve saat henüz yayımlanmadı", "Day and time not published yet")}</span>
                                  {/* Shown as returned by the server. The
                                      department's free-text note remains
                                      context; eligibility is a server verdict. */}
                                  {section.constraint ? <span className="mt-1 block rounded bg-muted/50 px-1.5 py-1 text-[11px] leading-snug text-muted-foreground">{localizedRestrictionReason(section.constraint, t)}</span> : null}
                                </div>
                              );
                            }) : <p className="text-xs text-muted-foreground">{t("Bu ders için şube bulunamadı.", "No sections found for this course.")}</p>}
                        </div> : null}
                      </div>
                    );
                  })}
                </div>
              </> : null}
            </CardContent></Card>

            {activeStep === "courses" ? <Card className="border-primary/30 bg-primary/5"><CardContent className="flex flex-wrap items-center justify-between gap-3 p-4"><div><p className="font-semibold">{t("Programını oluşturmaya hazır mısın?", "Ready to build your timetable?")}</p><p className="mt-1 text-xs leading-5 text-muted-foreground">{whatIf ? t("Keşif modundasın; sonuçları kayıt öncesi tekrar doğrula.", "You are in what-if mode; verify the result before registering.") : t("Seçtiğin tüm zorunlu dersler için eksiksiz bir sonuç aranır.", "A complete result is requested for every selected required course.")}</p></div><Button onClick={() => void generateSchedule().then((success) => { if (success) navigateStep("timetable"); })} disabled={busy || !catalogCourses.length}>{generateProgress ? t("Oluşturuluyor…", "Generating…") : t("Programımı gör", "See my timetable")}</Button></CardContent></Card> : null}

            {entries.length ? <Card data-ph-mask><CardHeader className="pb-3"><CardTitle className="text-base">{t("Eklenen dersler", "Added courses")}</CardTitle></CardHeader><CardContent className="space-y-2">
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
          <section aria-label={t("Haftalık program", "Weekly schedule")} className={cn(activeStep === "timetable" ? "min-w-0 space-y-3 xl:flex xl:min-h-0 xl:flex-col xl:gap-3 xl:space-y-0" : "hidden")}>
            {generationError || unscheduledCourses.length ? <div className="rounded-xl border border-amber-500/35 bg-amber-500/8 p-3 text-sm" role="status"><p className="font-semibold">{t("Program tamamlanamadı", "Schedule needs attention")}</p><p className="mt-1 text-xs leading-5 text-muted-foreground">{generationError || t("Bazı seçili dersler yerleştirilemedi.", "Some selected courses could not be placed.")}</p>{unscheduledCourses.length ? <p className="mt-2 text-xs font-medium" data-ph-mask>{t("Eksik dersler", "Unscheduled courses")}: {unscheduledCourses.join(", ")}</p> : null}</div> : null}
            <Card className="min-h-[420px] overflow-hidden xl:flex xl:min-h-[520px] xl:flex-1 xl:flex-col">
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
              <CardContent className="min-h-[360px] p-0 xl:min-h-[440px] xl:flex-1" data-tour="grid">
                <div className="min-h-[340px] border-t p-3 xl:min-h-[420px]" data-ph-mask>
                  <PlannerCalendar
                    entries={entries}
                    onSelectEntry={selectCalendarEntry}
                    onChangeBlock={changePersonalBlock}
                    locale={locale}
                    labels={{ week: t("Haftalık program", "Weekly schedule"), selectEntry: t("Ayrıntıları aç", "Open details"), room: t("Derslik", "Room"), noRoom: "TBA", course: t("Ders", "Course"), block: t("Kişisel blok", "Personal block") }}
                  />
                </div>
                {selectedEntry ? (() => {
                  const selectedCourse = selectedEntry.kind === "course"
                    ? catalogCourses.find((course) => courseIdentity(course.code) === courseIdentity(selectedEntry.code) || courseIdentity(course.rawCode) === courseIdentity(selectedEntry.code))
                    : null;
                  const selectedSections = selectedCourse ? sectionsByCourse[courseIdentity(selectedCourse.rawCode)] ?? [] : [];
                  return (
                    <div className="border-t bg-muted/20 p-4" role="dialog" aria-label={t("Program girdisi ayrıntıları", "Schedule entry details")} data-ph-mask>
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <h3 className="font-semibold">{selectedEntry.kind === "block" ? selectedEntry.name || t("Kişisel blok", "Personal block") : `${selectedEntry.code} · ${localizedCourseName(selectedEntry.name, locale)}`}</h3>
                          <p className="mt-1 text-xs text-muted-foreground">{dayLabel(selectedEntry.day)} · {formatItemRange(selectedEntry)}{selectedEntry.section ? ` · ${t("Şube", "Section")} ${selectedEntry.section}` : ""}{selectedEntry.room ? ` · ${selectedEntry.room}` : ""}</p>
                        </div>
                        <Button size="sm" variant="outline" className="shrink-0 text-destructive" onClick={removeSelectedEntry}><Trash2Icon />{t("Kaldır", "Remove")}</Button>
                      </div>
                      {selectedCourse ? (
                        <div className="mt-3 space-y-2">
                          <p className="text-xs font-medium">{t("Alternatif şubeler", "Section alternatives")}</p>
                          {selectedSections.length ? selectedSections.map((section) => {
                            const check = sectionAllowed(selectedCourse, section);
                            const current = section.section === selectedEntry.section;
                            /* The nested helper is invoked only by the click
                               handler; the refs rule cannot follow that path. */
                            /* eslint-disable react-hooks/refs */
                            return (
                              <button key={section.section} type="button" disabled={current || (!check.allowed && !whatIf)} onClick={() => replaceSelectedCourseSection(selectedCourse, section)} className={cn("flex w-full items-center justify-between gap-3 rounded-lg border bg-background p-2 text-left text-xs", current && "border-primary bg-primary/5", !check.allowed && !whatIf && "opacity-50")}>
                                <span><span className="font-semibold">{t("Şube", "Section")} {section.section}</span><span className="ml-2 text-muted-foreground">{section.meetings.length ? section.meetings.map((meeting) => `${dayLabel(meeting.day)} ${formatItemRange(meeting)}`).join(" / ") : t("Saat yayımlanmadı", "Time not published")}</span></span>
                                <span className="shrink-0 text-[10px] text-muted-foreground">{current ? t("Seçili", "Selected") : check.status === "verified" && check.allowed ? t("Uygun", "Eligible") : t("Doğrulanmadı", "Unverified")}</span>
                              </button>
                            );
                            /* eslint-enable react-hooks/refs */
                          }) : <p className="text-xs text-muted-foreground">{t("Alternatif şube bilgisi henüz yüklenmedi.", "Section alternatives have not loaded yet.")}</p>}
                        </div>
                      ) : <p className="mt-3 text-xs text-muted-foreground">{t("Kişisel bloklar takvimde sürüklenebilir ve boyutlandırılabilir.", "Personal blocks can be dragged and resized on the calendar.")}</p>}
                    </div>
                  );
                })() : null}
              </CardContent>
            </Card>

            <div className="flex flex-wrap gap-2 xl:shrink-0 [&_button]:h-9">
              <Button data-tour="build" onClick={() => void generateSchedule("manual")} disabled={busy}>{generateProgress ? t(`Program oluşturuluyor… (${generateProgress.done}/${generateProgress.total})`, `Generating… (${generateProgress.done}/${generateProgress.total})`) : t("Programı oluştur", "Generate schedule")}</Button>
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
              <Button variant="outline" onClick={exportCsv} disabled={exportBlocked}><DownloadIcon />{t("CSV olarak indir", "Download as CSV")}</Button>
              <div className="flex flex-wrap items-end gap-2 rounded-md border bg-background px-2 py-1.5">
                <div><Label htmlFor="planner-semester-start" className="text-[10px] text-muted-foreground">{t("Dönem başlangıcı", "Term starts")}</Label><Input id="planner-semester-start" type="date" value={semesterStart} onChange={(event) => { setSemesterStart(event.target.value); setSemesterDatesConfirmed(false); }} className="h-8 w-[132px] text-xs" /></div>
                <div><Label htmlFor="planner-semester-end" className="text-[10px] text-muted-foreground">{t("Dönem bitişi", "Term ends")}</Label><Input id="planner-semester-end" type="date" value={semesterEnd} onChange={(event) => { setSemesterEnd(event.target.value); setSemesterDatesConfirmed(false); }} className="h-8 w-[132px] text-xs" /></div>
                <label className="flex max-w-[220px] items-center gap-2 text-[10px] leading-tight text-muted-foreground"><input type="checkbox" checked={semesterDatesConfirmed} onChange={(event) => setSemesterDatesConfirmed(event.target.checked)} />{t("Tarihlerin döneme uyduğunu onaylıyorum", "I confirm these dates match the term")}</label>
                <p className="max-w-[220px] text-[10px] leading-tight text-muted-foreground">{t("Tarihler ön doldurulmuş tahminlerdir; dışa aktarmadan önce doğrula.", "Dates are prefilled estimates; verify them before exporting.")}</p>
                <Button variant="outline" onClick={() => void exportIcs()} disabled={exportBlocked || !semesterDatesConfirmed}><DownloadIcon />{t("Takvim (.ics)", "Calendar (.ics)")}</Button>
              </div>
              <Button variant="outline" onClick={exportWallpaper} disabled={exportBlocked}><DownloadIcon />{t("4K duvar kâğıdı", "4K wallpaper")}</Button>
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
