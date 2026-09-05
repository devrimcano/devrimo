"use client";

import { useEffect, useMemo, useState } from "react";
import {
  ClipboardIcon, DownloadIcon, HeartIcon,
  Loader2Icon, PlusIcon, RotateCcwIcon, SaveIcon, SparklesIcon, Trash2Icon,
} from "lucide-react";
import { toast } from "sonner";
import { useLocale } from "@/components/locale-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import { jsonFetch } from "@/lib/api/fetcher";
import { captureProductEvent, captureRequestFailure } from "@/components/posthog-analytics";
import { AVAILABLE_TERMS, formatAcademicTerm } from "@/lib/campus";

type Day = "Mon" | "Tue" | "Wed" | "Thu" | "Fri";
type Entry = { id: string; code: string; name: string; section: string; day: Day; start: number; duration: number; room: string; credits: number; color: number; kind: "course" | "block" };
type SavedPlan = { entries: Entry[]; term: string; department: string; surname: string; emptyDay: string; avoidConflicts: boolean };
type CatalogCourse = { code: string; name: string; credits: number; rawCode: string };
type CatalogSection = { section: string; instructor: string; meetings: { day: Day; start: number; duration: number; room: string }[] };
type AiPlanCourse = { code?: string; name?: string; credits?: number; sections?: unknown };

const DAYS: Day[] = ["Mon", "Tue", "Wed", "Thu", "Fri"];
const HOURS = Array.from({ length: 10 }, (_, index) => index + 8);
const COLORS = ["bg-rose-500/14 border-rose-500/35 text-rose-950 dark:text-rose-100", "bg-sky-500/14 border-sky-500/35 text-sky-950 dark:text-sky-100", "bg-amber-500/16 border-amber-500/40 text-amber-950 dark:text-amber-100", "bg-emerald-500/14 border-emerald-500/35 text-emerald-950 dark:text-emerald-100", "bg-violet-500/14 border-violet-500/35 text-violet-950 dark:text-violet-100"];
const STORAGE_KEY = "devrimo:schedule:v1";

const keyValue = (record: Record<string, unknown>, candidates: string[]) => {
  const key = Object.keys(record).find((item) => candidates.some((candidate) => item.toLowerCase().replace(/[^a-z0-9]/g, "").includes(candidate)));
  return key ? record[key] : undefined;
};

function objectRecords(value: unknown): Record<string, unknown>[] {
  if (Array.isArray(value)) return value.flatMap(objectRecords);
  if (!value || typeof value !== "object") return [];
  const record = value as Record<string, unknown>;
  if (keyValue(record, ["coursecode", "code", "coursename"])) return [record];
  return Object.values(record).flatMap(objectRecords);
}

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

function parseCourses(value: unknown): CatalogCourse[] {
  if (typeof value === "string") {
    try { return parseCourses(JSON.parse(value)); } catch {
      return value.split("\n").flatMap((line) => {
        const match = line.match(/\b(\d{7}|[A-Z]{2,5}\s*\d{3,4})\b\s*[-|:]?\s*(.*)/i);
        return match ? [{ code: match[1].replace(/([A-Z])(?=\d)/i, "$1 ").toUpperCase(), rawCode: match[1], name: match[2].replace(/\|.*/, "").trim() || match[1], credits: Number(line.match(/\b(\d(?:\.\d)?)\s*(?:credit|kredi)/i)?.[1] ?? 0) }] : [];
      });
    }
  }
  const seen = new Set<string>();
  return objectRecords(value).flatMap((record) => {
    const rawCode = String(keyValue(record, ["coursecode", "code"]) ?? "").trim();
    if (!rawCode || seen.has(rawCode)) return [];
    seen.add(rawCode);
    return [{ rawCode, code: rawCode.toUpperCase(), name: String(keyValue(record, ["coursename", "name", "title"]) ?? rawCode), credits: Number(keyValue(record, ["credit"]) ?? 0) }];
  });
}

// Full METU codes are globally unique, so a course's identity is its whole
// code. Reducing it to the final three digits would confuse a service course
// with a home-department one: two unrelated departments both have a 201.
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

function belongsToDepartment(course: CatalogCourse, department: string) {
  const digits = course.rawCode.replace(/\D/g, "");
  return digits.length !== 7 || digits.startsWith(department);
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
        return [{ section, instructor, meetings }];
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
      if (keyValue(candidate, ["day", "weekday", "gun"]) && keyValue(candidate, ["starttime", "start", "begin", "baslangic"])) meetingRecords.push(candidate);
      else Object.values(candidate).forEach(visitMeetings);
    };
    visitMeetings(scheduleValue);
    const structuredMeetings = meetingRecords.flatMap((meeting) => {
      const day = parseDay(String(keyValue(meeting, ["day", "weekday", "gun"]) ?? ""));
      const startText = String(keyValue(meeting, ["starttime", "start", "begin", "baslangic"]) ?? "");
      const endText = String(keyValue(meeting, ["endtime", "end", "finish", "bitis"]) ?? "");
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
    return [{ section, instructor, meetings }];
  });
}

function overlaps(a: Entry, b: Entry) {
  return a.day === b.day && a.start < b.start + b.duration && b.start < a.start + a.duration;
}

export function SchedulePlanner() {
  const { pick, locale } = useLocale();
  const t = (tr: string, en: string) => pick({ tr, en });
  const [entries, setEntries] = useState<Entry[]>([]);
  const [favorites, setFavorites] = useState<Entry[][]>([]);
  const [term, setTerm] = useState("20261");
  const [department, setDepartment] = useState("");
  const [departmentLabel, setDepartmentLabel] = useState("");
  const [departmentBusy, setDepartmentBusy] = useState(true);
  const [surname, setSurname] = useState("");
  const [emptyDay, setEmptyDay] = useState("");
  const [avoidConflicts, setAvoidConflicts] = useState(true);
  const [catalogCourses, setCatalogCourses] = useState<CatalogCourse[]>([]);
  const [catalogSections, setCatalogSections] = useState<CatalogSection[]>([]);
  const [selectedCourse, setSelectedCourse] = useState<CatalogCourse | null>(null);
  const [catalogSearch, setCatalogSearch] = useState("");
  const [catalogBusy, setCatalogBusy] = useState(false);
  const [activeStep, setActiveStep] = useState<"courses" | "sections" | "schedule" | null>(null);
  const [waitSeconds, setWaitSeconds] = useState(0);
  const [curriculumNotice, setCurriculumNotice] = useState("");
  const [aiSections, setAiSections] = useState<Record<string, CatalogSection[]>>({});
  const [poolDraft, setPoolDraft] = useState({ code: "", name: "" });
  const [draft, setDraft] = useState({ code: "", name: "", section: "1", day: "Mon" as Day, start: 9, duration: 1, room: "", credits: 3 });

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const raw = window.localStorage.getItem(`${STORAGE_KEY}:favorites`);
      if (raw) try { setFavorites(JSON.parse(raw) as Entry[][]); } catch { /* ignore corrupt local draft */ }
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (activeStep !== "courses") return;
    const timer = window.setInterval(() => setWaitSeconds((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [activeStep]);

  useEffect(() => {
    let cancelled = false;
    void jsonFetch<{ department_query: string | null; department_code: string | null }>("/api/schedule/student-context")
      .then((context) => {
        if (cancelled) return;
        setDepartmentLabel(context.department_query ?? "");
        // Resolved against the catalog server-side. Scanning the payload here
        // for any three-digit run used to pick up a year or a row count and
        // then quietly load another department's courses.
        setDepartment(context.department_code ?? "");
      })
      .catch((error) => {
        // The planner is driven imperatively rather than through TanStack
        // Query, so none of its failures reached the central query reporter.
        captureRequestFailure(error, { operation: "schedule.student_context", kind: "query" });
        if (!cancelled) toast.error(error instanceof Error ? error.message : t("Bölüm bilgisi alınamadı.", "Department could not be loaded."));
      })
      .finally(() => { if (!cancelled) setDepartmentBusy(false); });
    return () => { cancelled = true; };
  // The student context is stable for the lifetime of this page.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  const dayLabel = (day: Day) => ({ Mon: t("Pzt", "Mon"), Tue: t("Sal", "Tue"), Wed: t("Çar", "Wed"), Thu: t("Per", "Thu"), Fri: t("Cum", "Fri") })[day];

  function addEntry() {
    const code = draft.code.trim().toUpperCase().replace(/\s+/g, " ");
    if (!code) return toast.error(t("Ders kodu veya blok adı gerekli.", "A course code or block name is required."));
    const next: Entry = { ...draft, id: crypto.randomUUID(), code, name: draft.name.trim() || code, room: draft.room.trim(), color: uniqueCourses % COLORS.length, kind: code.startsWith("BLOCK:") ? "block" : "course" };
    if (emptyDay === next.day) return toast.error(t("Bu günü boş gün olarak seçtin.", "You selected this as your empty day."));
    if (avoidConflicts && entries.some((entry) => overlaps(entry, next))) return toast.error(t("Bu saat mevcut bir dersle çakışıyor.", "This time conflicts with an existing course."));
    setEntries((current) => [...current, next]);
    setDraft((current) => ({ ...current, code: "", name: "", room: "" }));
  }

  async function loadCatalogCourses() {
    if (!department.trim() || !term.trim()) return toast.error(t("Bölüm kodu ve dönem gerekli.", "Department and term are required."));
    setCatalogBusy(true); setActiveStep("courses"); setSelectedCourse(null); setCatalogSections([]);
    try {
      const response = await jsonFetch<{ data: unknown }>(`/api/schedule/courses?department=${encodeURIComponent(department.trim())}&semester=${encodeURIComponent(term)}`);
      const courses = parseCourses(response.data).filter((course) => belongsToDepartment(course, department.trim()));
      setCatalogCourses(courses);
      if (!courses.length) toast.error(t("Bu bölüm ve dönem için ders bulunamadı.", "No courses were found for this department and term."));
    } catch (error) {
      captureRequestFailure(error, { operation: "schedule.courses", kind: "query" });
      toast.error(t(`Ders listesi alınamadı: ${error instanceof Error ? error.message : "Bilinmeyen hata"}`, `Course list failed: ${error instanceof Error ? error.message : "Unknown error"}`));
    }
    finally { setCatalogBusy(false); setActiveStep(null); }
  }

  async function requestAiPlan(courses: CatalogCourse[]) {
    // The slowest thing a student waits on in this app, and the only one
    // backed by the agent. Its outcome was previously visible only as a toast.
    const startedAt = Date.now();
    let response: { courses?: AiPlanCourse[]; warnings?: string[]; source?: string };
    try {
      response = await jsonFetch<{ courses?: AiPlanCourse[]; warnings?: string[]; source?: string }>("/api/schedule/ai-plan", {
        method: "POST",
        body: {
          department: department.trim(),
          semester: term,
          courses: courses.map((course) => ({ code: course.rawCode })),
        },
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
    const sectionMap: Record<string, CatalogSection[]> = {};
    const verified = (response.courses ?? []).flatMap((item) => {
      const rawCode = String(item.code ?? "").trim();
      if (!rawCode) return [];
      const candidate = { rawCode, code: rawCode.toUpperCase(), name: String(item.name ?? rawCode), credits: Number(item.credits ?? 0) };
      sectionMap[courseIdentity(rawCode)] = parseSections(item.sections ?? []);
      return [candidate];
    });
    setAiSections((current) => ({ ...current, ...sectionMap }));
    const warnings = (response.warnings ?? []).filter((warning): warning is string => typeof warning === "string");
    captureProductEvent("schedule_plan_completed", {
      result: "success",
      requested_courses: courses.length,
      returned_courses: verified.length,
      warnings: warnings.length,
      duration_seconds: (Date.now() - startedAt) / 1000,
    });
    return { courses: verified, warnings, sections: sectionMap };
  }

  function addPoolCourse() {
    const rawCode = poolDraft.code.toUpperCase().replace(/[^A-Z0-9]/g, "");
    if (!rawCode) return toast.error(t("Ders kodu gerekli.", "Course code is required."));
    if (catalogCourses.some((course) => courseIdentity(course.rawCode) === courseIdentity(rawCode))) return toast.error(t("Bu ders zaten listede.", "This course is already in the list."));
    setCatalogCourses((current) => [...current, { rawCode, code: rawCode, name: poolDraft.name.trim() || rawCode, credits: 0 }]);
    setPoolDraft({ code: "", name: "" });
  }

  function removePoolCourse(course: CatalogCourse) {
    const identity = courseIdentity(course.rawCode);
    setCatalogCourses((current) => current.filter((item) => courseIdentity(item.rawCode) !== identity));
    setAiSections((current) => { const next = { ...current }; delete next[identity]; return next; });
    if (selectedCourse && courseIdentity(selectedCourse.rawCode) === identity) { setSelectedCourse(null); setCatalogSections([]); }
  }

  function clearCoursePool() {
    setCatalogCourses([]);
    setAiSections({});
    setSelectedCourse(null);
    setCatalogSections([]);
    setCatalogSearch("");
    setCurriculumNotice("");
    toast.success(t("Ders havuzu temizlendi.", "Course pool cleared."));
  }

  function removeScheduledCourse(courseEntries: Entry[]) {
    const ids = new Set(courseEntries.map((entry) => entry.id));
    setEntries((current) => current.filter((entry) => !ids.has(entry.id)));
  }

  async function loadRequiredCourses() {
    if (!department.trim()) return toast.error(t("Bölüm kodu gerekli.", "Department code is required."));
    setWaitSeconds(0);
    setCatalogBusy(true); setActiveStep("courses"); setCurriculumNotice(""); setSelectedCourse(null); setCatalogSections([]);
    try {
      const result = await requestAiPlan([]);
      setCatalogCourses(result.courses);
      setCurriculumNotice(result.courses.length
        ? t(`${result.courses.length} ders listelendi.${result.warnings.length ? ` ${result.warnings.join(" ")}` : ""}`, `${result.courses.length} courses listed.${result.warnings.length ? ` ${result.warnings.join(" ")}` : ""}`)
        : t("Bu dönem için kayıtlı zorunlu ders bulunamadı.", "No required courses found for this term."));
    } catch (error) {
      captureRequestFailure(error, { operation: "schedule.required_courses", kind: "query" });
      toast.error(t(`Alman gereken dersler getirilemedi: ${error instanceof Error ? error.message : "Bilinmeyen hata"}`, `Required courses failed: ${error instanceof Error ? error.message : "Unknown error"}`));
    } finally { setCatalogBusy(false); setActiveStep(null); }
  }

  async function loadSections(course: CatalogCourse) {
    setCatalogBusy(true); setActiveStep("sections"); setSelectedCourse(course); setCatalogSections([]);
    try {
      let sections = aiSections[courseIdentity(course.rawCode)] ?? [];
      if (!sections.length) {
        const courseDepartment = owningDepartment(course.rawCode, department);
        const response = await jsonFetch<{ data: unknown }>(`/api/schedule/courses/${encodeURIComponent(course.rawCode)}?department=${encodeURIComponent(courseDepartment)}&semester=${encodeURIComponent(term)}`);
        sections = parseSections(response.data);
      }
      setCatalogSections(sections);
      setAiSections((current) => ({ ...current, [courseIdentity(course.rawCode)]: sections }));
      if (!sections.length) toast.error(t("Bu ders için ODTÜ sisteminde şube bulunamadı.", "No sections were found for this course in METU's system."));
      else if (sections.every((section) => !section.meetings.length)) toast.warning(t(`${course.code} için ${sections.length} şube var; ODTÜ gün ve saatleri henüz yayımlamamış.`, `${sections.length} sections exist for ${course.code}, but METU has not published their days and times yet.`));
    } catch (error) {
      captureRequestFailure(error, { operation: "schedule.sections", kind: "query" });
      toast.error(t(`${course.code} şubeleri alınamadı: ${error instanceof Error ? error.message : "Bilinmeyen hata"}`, `${course.code} sections failed: ${error instanceof Error ? error.message : "Unknown error"}`));
    }
    finally { setCatalogBusy(false); setActiveStep(null); }
  }

  async function generateSchedule() {
    if (!catalogCourses.length) return toast.error(t("Önce dönem derslerini getir.", "Load semester courses first."));
    setCatalogBusy(true); setActiveStep("schedule");
    try {
      const generated: Entry[] = [];
      const missing: string[] = [];
      for (const course of catalogCourses) {
        const sections = aiSections[courseIdentity(course.rawCode)] ?? [];
        // Two independent preferences: the empty day is respected whether or
        // not conflict prevention is on, which is what addEntry already does.
        const chosen = sections.find((section) =>
          section.meetings.length > 0
          && section.meetings.every((meeting) => emptyDay !== meeting.day)
          && (!avoidConflicts || section.meetings.every((meeting) => {
            const candidate = { day: meeting.day, start: meeting.start, duration: meeting.duration } as Entry;
            return generated.every((entry) => !overlaps(entry, candidate));
          })));
        if (!chosen) { missing.push(course.code); continue; }
        generated.push(...chosen.meetings.map((meeting, index) => ({ id: crypto.randomUUID(), code: course.code, name: course.name, section: chosen.section, credits: index === 0 ? course.credits : 0, color: generated.length % COLORS.length, kind: "course" as const, ...meeting })));
      }
      setEntries(generated);
      if (missing.length) toast.warning(t(`${missing.join(", ")} için gün/saat verisi yok. Dersleri açıp şubeleri getir; ODTÜ henüz yayımlamadıysa program oluşturulamaz.`, `Day/time data is missing for ${missing.join(", ")}. Load each course's sections; a schedule cannot be generated until METU publishes the times.`));
      else toast.success(t("Çakışmasız program oluşturuldu.", "A conflict-free schedule was generated."));
    } catch (error) {
      toast.error(error instanceof Error ? error.message : t("Program oluşturulamadı.", "The schedule could not be generated."));
    } finally { setCatalogBusy(false); setActiveStep(null); }
  }

  function addCatalogSection(section: CatalogSection) {
    if (!selectedCourse) return;
    if (!section.meetings.length) return toast.warning(t("Bu şubenin gün ve saati ODTÜ tarafından henüz yayımlanmamış.", "METU has not published this section's day and time yet."));
    const additions = section.meetings.map((meeting, index) => ({ id: crypto.randomUUID(), code: selectedCourse.code, name: selectedCourse.name, section: section.section, credits: index === 0 ? selectedCourse.credits : 0, color: uniqueCourses % COLORS.length, kind: "course" as const, ...meeting }));
    if (avoidConflicts && additions.some((next) => entries.some((entry) => overlaps(entry, next)))) return toast.error(t("Bu şube mevcut programla çakışıyor.", "This section conflicts with your schedule."));
    setEntries((current) => [...current, ...additions]);
    toast.success(t(`${selectedCourse.code} şube ${section.section} eklendi.`, `${selectedCourse.code} section ${section.section} added.`));
  }

  function save() {
    const payload: SavedPlan = { entries, term, department, surname, emptyDay, avoidConflicts };
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
    toast.success(t("Program bu cihazda kaydedildi.", "Schedule saved on this device."));
  }

  function load() {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return toast.error(t("Kaydedilmiş program bulunamadı.", "No saved schedule was found."));
    try {
      const plan = JSON.parse(raw) as SavedPlan;
      setEntries(plan.entries ?? []); setTerm(plan.term); setDepartment(plan.department); setSurname(plan.surname); setEmptyDay(plan.emptyDay); setAvoidConflicts(plan.avoidConflicts);
      toast.success(t("Program yüklendi.", "Schedule loaded."));
    } catch { toast.error(t("Kayıt okunamadı.", "The saved schedule could not be read.")); }
  }

  function favorite() {
    if (!entries.length) return;
    const next = [...favorites, entries].slice(-10);
    setFavorites(next); window.localStorage.setItem(`${STORAGE_KEY}:favorites`, JSON.stringify(next));
    toast.success(t("Program favorilere eklendi.", "Schedule added to favorites."));
  }

  async function copySummary() {
    const summary = DAYS.map((day) => `${dayLabel(day)}: ${entries.filter((e) => e.day === day).sort((a, b) => a.start - b.start).map((e) => `${String(e.start).padStart(2, "0")}:40 ${e.code}-${e.section}${e.room ? ` (${e.room})` : ""}`).join(", ") || "—"}`).join("\n");
    await navigator.clipboard.writeText(summary); toast.success(t("Program özeti kopyalandı.", "Schedule summary copied."));
  }

  async function copyShareLink() {
    const encoded = btoa(unescape(encodeURIComponent(JSON.stringify(entries))));
    await navigator.clipboard.writeText(`${window.location.origin}/schedule#plan=${encoded}`);
    toast.success(t("Paylaşım bağlantısı kopyalandı.", "Share link copied."));
  }

  useEffect(() => {
    if (!window.location.hash.startsWith("#plan=")) return;
    const timer = window.setTimeout(() => {
      try { setEntries(JSON.parse(decodeURIComponent(escape(atob(window.location.hash.slice(6))))) as Entry[]); } catch { /* malformed shared plan */ }
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  function exportWallpaper() {
    const width = 3840, height = 2160;
    const cells = entries.map((e) => `<rect x="${460 + DAYS.indexOf(e.day) * 650}" y="${330 + (e.start - 8) * 170}" width="610" height="${e.duration * 160}" rx="24" fill="#e31837" opacity=".9"/><text x="${490 + DAYS.indexOf(e.day) * 650}" y="${390 + (e.start - 8) * 170}" fill="white" font-size="36" font-family="Arial" font-weight="700">${e.code.replace(/[<>&]/g, "")}</text>`).join("");
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><rect width="100%" height="100%" fill="#171312"/><text x="180" y="170" fill="white" font-size="72" font-family="Arial" font-weight="700">Devrimo · ${formatAcademicTerm(term, locale)}</text>${DAYS.map((d, i) => `<text x="${500 + i * 650}" y="285" fill="#aaa" font-size="36" font-family="Arial">${dayLabel(d)}</text>`).join("")}${cells}</svg>`;
    const link = document.createElement("a"); link.href = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" })); link.download = "devrimo-schedule-4k.svg"; link.click(); URL.revokeObjectURL(link.href);
  }

  return (
    <div className="h-full overflow-y-auto bg-[radial-gradient(circle_at_85%_0%,rgb(227_24_55/8%),transparent_32%)] px-4 py-6 sm:px-6 lg:px-8">
      <div className="mx-auto max-w-[1500px] space-y-5">
        <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
          <div><p className="text-sm font-semibold text-primary">{t("Akademik planlayıcı", "Academic planner")}</p><h1 className="text-3xl font-semibold tracking-tight">{t("Ders programı", "Schedule")}</h1><p className="mt-1 max-w-2xl text-sm text-muted-foreground">{t("Derslerini ekle, çakışmaları gör, programını kaydet ve paylaş.", "Add courses, spot conflicts, save and share your schedule.")}</p></div>
          <div className="flex flex-wrap gap-2"><Button variant="outline" onClick={load}>{t("Yükle", "Load")}</Button><Button variant="outline" onClick={save}><SaveIcon />{t("Kaydet", "Save")}</Button><Button variant="outline" onClick={() => setEntries([])}><RotateCcwIcon />{t("Temizle", "Clear")}</Button></div>
        </div>

        <div className="grid gap-5 xl:grid-cols-[330px_minmax(0,1fr)]">
          <aside className="space-y-5">
            <Card><CardHeader><CardTitle>{t("Tercihler", "Preferences")}</CardTitle></CardHeader><CardContent className="grid gap-3">
              <Field label={t("Akademik dönem", "Academic term")}><select value={term} onChange={(e) => setTerm(e.target.value)} className="h-10 w-full rounded-md border bg-background px-3 text-sm">{AVAILABLE_TERMS.map((item) => <option key={item.value} value={item.value}>{item[locale]}</option>)}</select></Field>
              <Field label={t("Soyadı", "Surname")}><Input value={surname} onChange={(e) => setSurname(e.target.value)} placeholder={t("Şube kısıtlamaları için isteğe bağlı", "Optional for section restrictions")} /></Field>
              <Field label={t("Bölüm", "Department")}><Input value={departmentBusy ? t("Alınıyor…", "Loading…") : departmentLabel || department || t("Bulunamadı", "Not found")} readOnly aria-readonly="true" className="bg-muted/40" /></Field>
              <Field label={t("Boş gün", "Empty day")}><select value={emptyDay} onChange={(e) => setEmptyDay(e.target.value)} className="h-10 w-full rounded-md border bg-background px-3 text-sm"><option value="">{t("Fark etmez", "No preference")}</option>{DAYS.map((d) => <option key={d} value={d}>{dayLabel(d)}</option>)}</select></Field>
              <Toggle label={t("Çakışmaları engelle", "Prevent conflicts")} checked={avoidConflicts} onChange={setAvoidConflicts} />
            </CardContent></Card>

            <Card><CardHeader><CardTitle>{t("Dönem dersleri", "Semester courses")}</CardTitle></CardHeader><CardContent className="grid gap-3">
              <Button onClick={() => void loadRequiredCourses()} disabled={catalogBusy || departmentBusy || !department}><SparklesIcon />{activeStep === "courses" ? t("Dersler belirleniyor…", "Finding courses…") : t("Almam gereken dersleri getir", "Load required courses")}</Button>
              <Button variant="outline" onClick={() => void loadCatalogCourses()} disabled={catalogBusy}>{t("Bölümde açılan tüm dersler", "All offered department courses")}</Button>
              {activeStep === "courses" ? <div className="overflow-hidden rounded-xl border bg-primary/5 p-3" role="status" aria-live="polite"><div className="flex items-center gap-3"><span className="relative flex size-9 shrink-0 items-center justify-center rounded-full bg-primary/10"><span className="absolute inset-0 animate-ping rounded-full bg-primary/10" /><Loader2Icon className="relative size-5 animate-spin text-primary" /></span><div className="min-w-0 flex-1"><p className="text-sm font-medium">{t("Ders listen hazırlanıyor", "Preparing your course list")}</p><p className="mt-0.5 text-xs text-muted-foreground">{waitSeconds < 15 ? t("Öğrenci bilgileri kontrol ediliyor…", "Checking student information…") : waitSeconds < 40 ? t("Müfredat dersleri eşleştiriliyor…", "Matching curriculum courses…") : t(`${formatAcademicTerm(term, locale)} dersleri kontrol ediliyor…`, `Checking courses for ${formatAcademicTerm(term, locale)}…`)}</p></div><span className="text-xs tabular-nums text-muted-foreground">{waitSeconds} sn</span></div><div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-primary transition-[width] duration-1000 ease-out" style={{ width: `${Math.min(92, 10 + waitSeconds * 1.35)}%` }} /></div><p className="mt-2 text-[11px] text-muted-foreground">{t("Sayfayı açık bırak; sonuç hazır olduğunda liste otomatik görünecek.", "Keep this page open; the list will appear automatically when ready.")}</p></div> : null}
              {curriculumNotice ? <p className="rounded-lg border bg-muted/30 p-2 text-xs leading-5 text-muted-foreground">{curriculumNotice}</p> : null}
              <div className="rounded-lg border bg-muted/20 p-2"><p className="mb-2 text-xs font-medium">{t("Ders havuzuna elle ekle", "Add manually to course pool")}</p><div className="grid gap-2"><Input value={poolDraft.code} onChange={(e) => setPoolDraft({ ...poolDraft, code: e.target.value })} placeholder={t("Örn. CENG 201 veya 5670201", "e.g. CENG 201 or 5670201")} /><Input value={poolDraft.name} onChange={(e) => setPoolDraft({ ...poolDraft, name: e.target.value })} placeholder={t("Ders adı (isteğe bağlı)", "Course name (optional)")} /><Button size="sm" variant="outline" onClick={addPoolCourse}><PlusIcon />{t("Ders havuzuna ekle", "Add to course pool")}</Button></div></div>
              {catalogCourses.length ? <><div className="flex items-center gap-2"><Input value={catalogSearch} onChange={(e) => setCatalogSearch(e.target.value)} placeholder={t("Ders kodu veya adı ara", "Search course code or name")} /><Button variant="outline" className="shrink-0 text-destructive" onClick={clearCoursePool}><Trash2Icon />{t("Tümünü sil", "Clear all")}</Button></div><div className="max-h-64 space-y-1 overflow-y-auto">{catalogCourses.filter((course) => `${course.code} ${course.name}`.toLowerCase().includes(catalogSearch.toLowerCase())).map((course) => <div key={course.rawCode} className={cn("flex items-center gap-1 rounded-lg border p-1 transition hover:bg-accent", selectedCourse?.rawCode === course.rawCode && "border-primary bg-primary/5")}><button onClick={() => void loadSections(course)} className="min-w-0 flex-1 p-1 text-left text-sm"><span className="block font-semibold">{course.code}</span><span className="block truncate text-xs text-muted-foreground">{course.name}</span></button><Button size="icon" variant="ghost" aria-label={t(`${course.code} dersini havuzdan çıkar`, `Remove ${course.code} from course pool`)} onClick={() => removePoolCourse(course)}><Trash2Icon /></Button></div>)}</div></> : null}
              {activeStep === "sections" ? <p className="text-xs text-muted-foreground">{t("Şubeler ve saatler getiriliyor…", "Loading sections and times…")}</p> : null}
              {selectedCourse && catalogSections.length ? <div className="space-y-2 border-t pt-3"><p className="text-sm font-semibold">{selectedCourse.code} · {t("Şubeler", "Sections")}</p>{catalogSections.map((section) => <button key={section.section} onClick={() => addCatalogSection(section)} className="w-full rounded-lg border p-2 text-left text-sm transition hover:border-primary/40 hover:bg-primary/5"><span className="font-semibold">{t("Şube", "Section")} {section.section}</span>{section.instructor ? <span className="ml-2 text-xs text-muted-foreground">{section.instructor}</span> : null}<span className="mt-1 block text-xs text-muted-foreground">{section.meetings.length ? section.meetings.map((meeting) => `${dayLabel(meeting.day)} ${String(meeting.start).padStart(2,"0")}:40${meeting.room ? ` · ${meeting.room}` : ""}`).join(" / ") : t("Gün ve saat henüz yayımlanmadı", "Day and time not published yet")}</span></button>)}</div> : null}
            </CardContent></Card>

            <Card><CardHeader><CardTitle>{t("Elle ders veya blok ekle", "Add course or block manually")}</CardTitle></CardHeader><CardContent className="grid gap-3">
              <div className="grid grid-cols-2 gap-2"><Field label={t("Kod", "Code")}><Input value={draft.code} onChange={(e) => setDraft({ ...draft, code: e.target.value })} placeholder="MATH 260" /></Field><Field label={t("Şube", "Section")}><Input value={draft.section} onChange={(e) => setDraft({ ...draft, section: e.target.value })} /></Field></div>
              <Field label={t("Ders adı", "Course name")}><Input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} placeholder={t("Temel Lineer Cebir", "Basic Linear Algebra")} /></Field>
              <div className="grid grid-cols-3 gap-2"><Field label={t("Gün", "Day")}><select value={draft.day} onChange={(e) => setDraft({ ...draft, day: e.target.value as Day })} className="h-10 w-full rounded-md border bg-background px-2 text-sm">{DAYS.map((d) => <option key={d} value={d}>{dayLabel(d)}</option>)}</select></Field><Field label={t("Başlangıç", "Start")}><select value={draft.start} onChange={(e) => setDraft({ ...draft, start: Number(e.target.value) })} className="h-10 w-full rounded-md border bg-background px-2 text-sm">{HOURS.map((h) => <option key={h} value={h}>{String(h).padStart(2,"0")}:40</option>)}</select></Field><Field label={t("Süre", "Hours")}><select value={draft.duration} onChange={(e) => setDraft({ ...draft, duration: Number(e.target.value) })} className="h-10 w-full rounded-md border bg-background px-2 text-sm">{[1,2,3].map((h) => <option key={h}>{h}</option>)}</select></Field></div>
              <div className="grid grid-cols-2 gap-2"><Field label={t("Derslik", "Room")}><Input value={draft.room} onChange={(e) => setDraft({ ...draft, room: e.target.value })} placeholder="M-13" /></Field><Field label={t("Kredi", "Credits")}><Input type="number" min={0} max={10} value={draft.credits} onChange={(e) => setDraft({ ...draft, credits: Number(e.target.value) })} /></Field></div>
              <Button onClick={addEntry}><PlusIcon />{t("Programa ekle", "Add to schedule")}</Button>
            </CardContent></Card>
          </aside>

          <main className="min-w-0 space-y-5">
            <div className="grid grid-cols-3 gap-3">{[[t("Toplam kredi", "Total credits"), totalCredits], [t("Haftalık saat", "Weekly hours"), totalHours], [t("Ders", "Courses"), uniqueCourses]].map(([label, value]) => <Card key={String(label)}><CardContent className="p-4"><p className="text-xs text-muted-foreground">{label}</p><p className="mt-1 text-2xl font-semibold">{value}</p></CardContent></Card>)}</div>
            <Card className="overflow-hidden"><CardHeader className="flex-row items-center justify-between gap-3"><div><CardTitle>{formatAcademicTerm(term, locale)}</CardTitle><p className="mt-1 text-xs text-muted-foreground">{conflicts.size ? t(`${conflicts.size} çakışan oturum var`, `${conflicts.size} conflicting sessions`) : t("Çakışma yok", "No conflicts")}</p></div><div className="flex gap-1"><Button size="icon" variant="ghost" aria-label={t("Favoriye ekle", "Favorite")} onClick={favorite}><HeartIcon /></Button><Button size="icon" variant="ghost" aria-label={t("Özeti kopyala", "Copy summary")} onClick={() => void copySummary()}><ClipboardIcon /></Button></div></CardHeader><CardContent className="overflow-x-auto p-0">
              <div className="grid min-w-[820px] grid-cols-[72px_repeat(5,minmax(140px,1fr))] border-t text-sm">
                <div className="border-b border-r bg-muted/30" />{DAYS.map((d) => <div key={d} className={cn("border-b border-r px-3 py-2 text-center font-semibold", emptyDay === d && "bg-primary/7 text-primary")}>{dayLabel(d)}</div>)}
                {HOURS.flatMap((hour) => [<div key={`h-${hour}`} className="border-b border-r bg-muted/30 px-2 py-3 text-xs tabular-nums text-muted-foreground">{String(hour).padStart(2,"0")}:40</div>, ...DAYS.map((day) => { const here = entries.filter((e) => e.day === day && e.start === hour); return <div key={`${day}-${hour}`} className={cn("relative min-h-20 border-b border-r p-1", emptyDay === day && "bg-muted/20")}>{here.map((entry) => <button key={entry.id} onClick={() => setEntries((current) => current.filter((item) => item.id !== entry.id))} title={t("Kaldırmak için tıkla", "Click to remove")} style={{ minHeight: `${entry.duration * 4.5}rem` }} className={cn("relative z-10 w-full rounded-lg border p-2 text-left shadow-sm transition hover:-translate-y-0.5 hover:shadow-md", COLORS[entry.color], conflicts.has(entry.id) && "ring-2 ring-destructive")}><span className="block font-semibold">{entry.code} · {entry.section}</span><span className="mt-1 block text-xs opacity-80">{entry.name}</span>{entry.room ? <span className="mt-1 block text-xs font-medium">{entry.room}</span> : null}</button>)}</div>; })] )}
              </div>
            </CardContent></Card>

            <div className="flex flex-wrap gap-2"><Button onClick={() => void generateSchedule()} disabled={catalogBusy}><SparklesIcon />{activeStep === "schedule" ? t("Program oluşturuluyor…", "Generating schedule…") : t("Programı oluştur", "Generate schedule")}</Button><Button variant="outline" onClick={() => void copyShareLink()}><ClipboardIcon />{t("Paylaşım bağlantısı", "Share link")}</Button><Button variant="outline" onClick={() => window.print()}><DownloadIcon />{t("Programı dışa aktar", "Export schedule")}</Button><Button variant="outline" onClick={exportWallpaper}><DownloadIcon />{t("4K duvar kâğıdı", "4K wallpaper")}</Button>{favorites.length ? <Button variant="ghost" onClick={() => setEntries(favorites[(favorites.findIndex((plan) => plan === entries) + 1) % favorites.length] ?? favorites[0])}>{t("Sonraki favori", "Next favorite")}</Button> : null}</div>
            {entries.length > 0 && <Card><CardHeader><CardTitle>{t("Eklenen dersler", "Added courses")}</CardTitle></CardHeader><CardContent className="space-y-2">{scheduledCourseGroups.map((courseEntries) => { const entry = courseEntries[0]; return <div key={`${entry.code}-${entry.section}`} className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2"><div className="min-w-0"><p className="truncate font-medium">{entry.code} · {entry.name}</p><p className="text-xs text-muted-foreground">{courseEntries.map((meeting) => `${dayLabel(meeting.day)} ${String(meeting.start).padStart(2,"0")}:40`).join(" / ")} · {t("Şube", "Section")} {entry.section}</p></div><Button size="icon" variant="ghost" aria-label={t("Dersi kaldır", "Remove course")} onClick={() => removeScheduledCourse(courseEntries)}><Trash2Icon /></Button></div>; })}</CardContent></Card>}
          </main>
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) { return <div className="space-y-1.5"><Label>{label}</Label>{children}</div>; }
function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (value: boolean) => void }) { return <div className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2"><Label className="leading-5">{label}</Label><Switch checked={checked} onCheckedChange={onChange} /></div>; }
