"use client";

import { useEffect, useId, useState, type Dispatch, type ReactNode, type SetStateAction } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BookOpenIcon,
  CheckCircle2Icon,
  ChevronLeftIcon,
  ChevronRightIcon,
  Clock3Icon,
  HistoryIcon,
  ListChecksIcon,
  Loader2Icon,
  RefreshCwIcon,
  RotateCcwIcon,
  SaveIcon,
  SendIcon,
  ShieldAlertIcon,
  SlidersHorizontalIcon,
  UploadCloudIcon,
} from "lucide-react";
import { toast } from "sonner";
import { useLocale } from "@/components/locale-provider";
import {
  EmptyState,
  ErrorState,
  PanelHeader,
  StatusBadge,
  formatDate,
} from "@/components/admin/admin-shared";
import { SectionNav } from "@/components/admin/panels/knowledge-workspace";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Progress } from "@/components/ui/progress";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { adminGet, adminMutate } from "@/lib/admin/client";
import type { AdminPrincipal } from "@/lib/admin/types";
import {
  conflictCount,
  type CatalogCompleteness,
  type CatalogCourseDetail,
  type CatalogCourseListResponse,
  type CatalogCourseRow,
  type CatalogDraft,
  type CatalogDraftPatch,
  type CatalogHistoryEntry,
  type CatalogImportJob,
  type CatalogImportsResponse,
  type CatalogMeeting,
  type CatalogOperationResponse,
  type CatalogRelease,
  type CatalogReleasesResponse,
  type CatalogSection,
  type CatalogSourceConflict,
  type CatalogSourceObservation,
  type CatalogSourceObservationDetail,
} from "@/lib/admin/catalog-types";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 25;
const DEFAULT_TERM = (() => {
  const date = new Date();
  const year = date.getFullYear();
  const month = date.getMonth() + 1;
  return month >= 8 ? `${year}1` : month <= 5 ? `${year - 1}2` : `${year - 1}3`;
})();

type CatalogView = "courses" | "imports" | "releases";
type CourseTab = "overview" | "sections" | "rules" | "sources" | "history";

type FormMeeting = {
  weekday: string;
  start_minute: string;
  end_minute: string;
  room: string;
  status: string;
  raw_label: string;
};

type FormInstructor = {
  name: string;
  position: string;
  researcher_id: string;
  resolution_status: string;
};

type FormRestriction = {
  kind: string;
  restriction_group: string;
  row_index: string;
  value_text: string;
  value_numeric: string;
  operator: string;
  minimum_grade: string;
  given_department: string;
  start_char: string;
  end_char: string;
  min_cgpa: string;
  max_cgpa: string;
  min_year: string;
  max_year: string;
  start_grade: string;
  end_grade: string;
  prior_course_code: string;
  program_code: string;
  curriculum_version: string;
  raw_text: string;
  verified: boolean;
  status: string;
};

type FormSection = {
  id: string;
  section_code: string;
  status: string;
  notes: string;
  syllabus_url: string;
  syllabus_available: boolean | null;
  meetings_status: string;
  meetings: FormMeeting[];
  instructors: FormInstructor[];
  restrictions: FormRestriction[];
};

type FormRequirement = {
  course_code: string;
  minimum_grade: string;
  requirement_type: string;
  position: string;
  raw_text: string;
};

type FormPrerequisiteGroup = {
  group_no: string;
  logic: string;
  program_code: string;
  curriculum_version: string;
  applicability: Record<string, unknown>;
  verified: boolean;
  raw_text: string;
  requirements: FormRequirement[];
};

type FormReplacement = {
  course_code: string;
  replacement_code: string;
  relation: string;
  program_code: string;
  curriculum_version: string;
  verified: boolean;
  raw_text: string;
};

type CourseForm = {
  title: string;
  department: string;
  credits: string;
  ects: string;
  level: string;
  availability: string;
  campus: string;
  is_thesis: boolean;
  sections: FormSection[];
  prerequisite_groups: FormPrerequisiteGroup[];
  replacements: FormReplacement[];
};

function asNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function text(value: unknown): string {
  return value === null || value === undefined ? "" : String(value);
}

function valueLabel(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

const MEETING_STATUS_OPTIONS = ["verified", "untimed", "unknown", "unpublished", "invalid"] as const;
type MeetingStatus = (typeof MEETING_STATUS_OPTIONS)[number];

function normalizeMeetingStatus(value: string | null | undefined): MeetingStatus {
  return MEETING_STATUS_OPTIONS.includes(value as MeetingStatus) ? value as MeetingStatus : "unknown";
}

function meetingStatusCopy(value: string | null | undefined) {
  switch (normalizeMeetingStatus(value)) {
    case "verified":
      return {
        label: { tr: "Doğrulanmış çizelge", en: "Verified schedule" },
        empty: { tr: "Toplantı zamanı doğrulandı.", en: "The meeting schedule was verified." },
      };
    case "untimed":
      return {
        label: { tr: "Planlanmış toplantı yok", en: "No scheduled meetings" },
        empty: { tr: "Bu şube açıkça zamansız olarak işaretlendi; zaman bilgisi eksik değil.", en: "This section is explicitly marked as having no scheduled meetings; the schedule is not missing." },
      };
    case "unpublished":
      return {
        label: { tr: "Çizelge yayımlanmadı", en: "Schedule not published" },
        empty: { tr: "Gün ve saat bilgisi henüz yayımlanmadı.", en: "Days and times have not been published yet." },
      };
    case "invalid":
      return {
        label: { tr: "Geçersiz çizelge", en: "Invalid schedule" },
        empty: { tr: "Kaynakta toplantı bilgisi var, ancak geçerli bir zaman olarak okunamadı.", en: "Meeting data was present in the source but could not be read as a valid time." },
      };
    default:
      return {
        label: { tr: "Toplantı zamanı bilinmiyor", en: "Meeting time unknown" },
        empty: { tr: "Bu şube için toplantı zamanı doğrulanmadı.", en: "The meeting time for this section has not been verified." },
      };
  }
}

function completenessStatus(value: CatalogCompleteness | string | null | undefined): string {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return "unknown";
  const components = ["listing", "details", "sections", "constraints", "prerequisites", "replacements"];
  const record = value as Record<string, unknown>;
  const known = components.filter((component) => typeof record[component] === "boolean");
  if (!known.length) return "unknown";
  if (known.every((component) => record[component] === true)) return "complete";
  if (known.some((component) => record[component] === true)) return "partial";
  return "missing";
}

/**
 * Draft details are intentionally sparse while a source import is in flight.
 * Normalize optional collections at the API boundary so an incomplete draft
 * still opens in the same typed editor as a published revision.
 */
function normalizeCatalogDetail(detail: CatalogCourseDetail, fallback: CatalogCourseRow): CatalogCourseDetail {
  const raw = detail as CatalogCourseDetail & Record<string, unknown>;
  const completeness: CatalogCompleteness = detail.completeness && typeof detail.completeness === "object" ? detail.completeness : {} as CatalogCompleteness;
  const catalog = detail._catalog && typeof detail._catalog === "object" ? detail._catalog : { release_id: null, components: {} };
  return {
    ...detail,
    course_code: text(detail.course_code) || fallback.course_code,
    term: text(detail.term),
    title: text(detail.title) || fallback.title,
    department: detail.department ?? fallback.department,
    local_credits: detail.local_credits ?? fallback.local_credits,
    ects: detail.ects ?? fallback.ects,
    level: detail.level ?? fallback.level,
    availability: detail.availability ?? fallback.availability,
    completeness,
    sections: Array.isArray(raw.sections) ? raw.sections : [],
    prerequisite_groups: Array.isArray(raw.prerequisite_groups) ? raw.prerequisite_groups : [],
    replacements: Array.isArray(raw.replacements) ? raw.replacements : [],
    source_observations: Array.isArray(raw.source_observations) ? raw.source_observations : [],
    history: Array.isArray(raw.history) ? raw.history : [],
    _catalog: catalog,
  };
}

/**
 * The admin detail response keeps the active published revision at the top
 * level and nests an active draft beside it.  Editors and draft previews must
 * read the draft's content fields first, while release/source metadata stays
 * attached to the published response below.
 */
function draftDetailFrom(detail: CatalogCourseDetail): CatalogCourseDetail {
  const draftData = detail.draft?.data;
  if (!draftData || typeof draftData !== "object") return detail;

  const source = draftData as Record<string, unknown>;
  const merged = { ...detail };
  const has = (field: string) => Object.prototype.hasOwnProperty.call(source, field);

  if (has("title")) merged.title = text(source.title);
  if (has("department")) merged.department = source.department === null || source.department === undefined ? null : text(source.department);
  if (has("local_credits")) merged.local_credits = asNumber(source.local_credits);
  if (has("ects")) merged.ects = asNumber(source.ects);
  if (has("level")) merged.level = source.level === null || source.level === undefined ? null : text(source.level);
  if (has("availability")) merged.availability = source.availability === null || source.availability === undefined ? null : text(source.availability);
  if (has("campus")) merged.campus = source.campus === null || source.campus === undefined ? null : text(source.campus);
  if (has("is_thesis")) merged.is_thesis = Boolean(source.is_thesis);
  if (has("sections")) merged.sections = Array.isArray(source.sections) ? source.sections as CatalogSection[] : [];
  if (has("prerequisite_groups")) {
    merged.prerequisite_groups = Array.isArray(source.prerequisite_groups)
      ? source.prerequisite_groups as CatalogCourseDetail["prerequisite_groups"]
      : [];
  }
  if (has("replacements")) {
    merged.replacements = Array.isArray(source.replacements)
      ? source.replacements as CatalogCourseDetail["replacements"]
      : [];
  }

  return merged;
}

function meetingLabel(meeting: CatalogMeeting, pick: (copy: { tr: string; en: string }) => string) {
  const days: Record<string, { tr: string; en: string }> = {
    "0": { tr: "Pzt", en: "Mon" }, "1": { tr: "Sal", en: "Tue" }, "2": { tr: "Çar", en: "Wed" },
    "3": { tr: "Per", en: "Thu" }, "4": { tr: "Cum", en: "Fri" }, "5": { tr: "Cmt", en: "Sat" }, "6": { tr: "Paz", en: "Sun" },
  };
  const day = meeting.weekday === null ? "?" : days[String(meeting.weekday)] ? pick(days[String(meeting.weekday)]) : String(meeting.weekday);
  const start = meeting.start_minute === null ? "?" : formatMinutes(meeting.start_minute);
  const end = meeting.end_minute === null ? "?" : formatMinutes(meeting.end_minute);
  return `${day} ${start}–${end}${meeting.room ? ` · ${meeting.room}` : ""}`;
}

function formatMinutes(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  const minutes = Math.max(0, Math.trunc(value));
  return `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
}

function formFromDetail(detail: CatalogCourseDetail): CourseForm {
  return {
    title: detail.title,
    department: detail.department ?? "",
    credits: (detail.local_credits ?? detail.credits) === null || (detail.local_credits ?? detail.credits) === undefined ? "" : String(detail.local_credits ?? detail.credits),
    ects: detail.ects === null ? "" : String(detail.ects),
    level: detail.level ?? "",
    availability: detail.availability ?? "",
    campus: detail.campus ?? "",
    is_thesis: Boolean(detail.is_thesis),
    sections: detail.sections.map((section) => ({
      id: section.id ?? "",
      section_code: section.section_code,
      status: section.status,
      notes: section.notes ?? "",
      syllabus_url: section.syllabus_url ?? "",
      syllabus_available: section.syllabus_available === true ? true : section.syllabus_available === false ? false : null,
      meetings_status: normalizeMeetingStatus(section.meetings_status),
      meetings: section.meetings.map((meeting) => ({
        weekday: meeting.weekday === null ? "" : String(meeting.weekday),
        start_minute: meeting.start_minute === null ? "" : String(meeting.start_minute),
        end_minute: meeting.end_minute === null ? "" : String(meeting.end_minute),
        room: meeting.room ?? "",
        status: meeting.status,
        raw_label: meeting.raw_label ?? "",
      })),
      instructors: section.instructors.map((instructor) => ({
        name: instructor.source_name ?? instructor.name ?? "",
        position: instructor.position ?? "",
        researcher_id: instructor.researcher_id === null || instructor.researcher_id === undefined ? "" : String(instructor.researcher_id),
        resolution_status: instructor.resolution_status,
      })),
      restrictions: section.restrictions.map((restriction) => ({
        kind: restriction.kind,
        restriction_group: restriction.restriction_group ?? "",
        row_index: restriction.row_index === undefined ? "" : String(restriction.row_index),
        value_text: restriction.value_text ?? "",
        value_numeric: restriction.value_numeric === null ? "" : String(restriction.value_numeric),
        operator: restriction.operator ?? "",
        minimum_grade: restriction.minimum_grade ?? "",
        given_department: restriction.given_department ?? "",
        start_char: restriction.start_char ?? "",
        end_char: restriction.end_char ?? "",
        min_cgpa: restriction.min_cgpa === null || restriction.min_cgpa === undefined ? "" : String(restriction.min_cgpa),
        max_cgpa: restriction.max_cgpa === null || restriction.max_cgpa === undefined ? "" : String(restriction.max_cgpa),
        min_year: restriction.min_year === null || restriction.min_year === undefined ? "" : String(restriction.min_year),
        max_year: restriction.max_year === null || restriction.max_year === undefined ? "" : String(restriction.max_year),
        start_grade: restriction.start_grade ?? "",
        end_grade: restriction.end_grade ?? "",
        prior_course_code: restriction.prior_course_code ?? "",
        program_code: restriction.program_code ?? "",
        curriculum_version: restriction.curriculum_version ?? "",
        raw_text: restriction.raw_text ?? "",
        verified: restriction.verified,
        status: restriction.status ?? (restriction.verified ? "verified" : "unknown"),
      })),
    })),
    prerequisite_groups: detail.prerequisite_groups.map((group) => ({
      group_no: String(group.group_no),
      logic: group.logic,
      program_code: group.program_code ?? "",
      curriculum_version: group.curriculum_version ?? "",
      requirements: group.requirements.map((requirement) => ({
        course_code: requirement.course_code,
        minimum_grade: requirement.minimum_grade ?? "",
        requirement_type: requirement.requirement_type,
        position: requirement.position === undefined ? "" : String(requirement.position),
        raw_text: requirement.raw_text ?? "",
      })),
      applicability: group.applicability && typeof group.applicability === "object" ? group.applicability : {},
      verified: Boolean(group.verified),
      raw_text: group.raw_text ?? "",
    })),
    replacements: detail.replacements.map((replacement) => ({
      course_code: text(replacement.course_code),
      replacement_code: text(replacement.replacement_code ?? replacement.equivalent_code ?? replacement.related_course_code),
      relation: text(replacement.relation ?? replacement.relationship_type),
      program_code: text(replacement.program_code),
      curriculum_version: text(replacement.curriculum_version),
      verified: Boolean(replacement.verified),
      raw_text: text(replacement.raw_text),
    })),
  };
}

function formToPatch(form: CourseForm): CatalogDraftPatch {
  return {
    title: form.title.trim(),
    department: form.department.trim() || null,
    local_credits: asNumber(form.credits),
    ects: asNumber(form.ects),
    level: form.level.trim() || null,
    availability: form.availability.trim() || null,
    campus: form.campus.trim() || null,
    is_thesis: form.is_thesis,
    sections: form.sections.map((section) => ({
      id: section.id || undefined,
      section_code: section.section_code.trim(),
      status: section.status.trim(),
      notes: section.notes.trim() || null,
      syllabus_url: section.syllabus_url.trim() || null,
      syllabus_available: section.syllabus_available,
      meetings_status: normalizeMeetingStatus(section.meetings_status),
      meetings: section.meetings.map((meeting) => ({
        weekday: asNumber(meeting.weekday),
        start_minute: asNumber(meeting.start_minute),
        end_minute: asNumber(meeting.end_minute),
        room: meeting.room.trim() || null,
        status: meeting.status.trim(),
        raw_label: meeting.raw_label.trim() || null,
      })),
      instructors: section.instructors.map((instructor) => ({
        source_name: instructor.name.trim(),
        position: instructor.position.trim() || null,
        researcher_id: asNumber(instructor.researcher_id),
        match_method: instructor.researcher_id.trim() ? "manual" : "unmatched",
        resolution_status: instructor.resolution_status.trim(),
      })),
      restrictions: section.restrictions.map((restriction) => ({
        restriction_group: restriction.restriction_group.trim() || null,
        row_index: asNumber(restriction.row_index) ?? 0,
        kind: restriction.kind.trim(),
        value_text: restriction.value_text.trim() || null,
        value_numeric: asNumber(restriction.value_numeric),
        operator: restriction.operator.trim() || null,
        minimum_grade: restriction.minimum_grade.trim() || null,
        given_department: restriction.given_department.trim() || null,
        start_char: restriction.start_char.trim() || null,
        end_char: restriction.end_char.trim() || null,
        min_cgpa: asNumber(restriction.min_cgpa),
        max_cgpa: asNumber(restriction.max_cgpa),
        min_year: asNumber(restriction.min_year),
        max_year: asNumber(restriction.max_year),
        start_grade: restriction.start_grade.trim() || null,
        end_grade: restriction.end_grade.trim() || null,
        prior_course_code: restriction.prior_course_code.trim() || null,
        program_code: restriction.program_code.trim() || null,
        curriculum_version: restriction.curriculum_version.trim() || null,
        raw_text: restriction.raw_text.trim() || null,
        verified: restriction.verified,
        status: restriction.status.trim() || (restriction.verified ? "verified" : "unknown"),
      })),
    })),
    prerequisite_groups: form.prerequisite_groups.map((group) => ({
      group_no: Number(group.group_no) || group.group_no,
      logic: group.logic.trim(),
      program_code: group.program_code.trim() || null,
      curriculum_version: group.curriculum_version.trim() || null,
      requirements: group.requirements.map((requirement) => ({
        course_code: requirement.course_code.trim(),
        minimum_grade: requirement.minimum_grade.trim() || null,
        requirement_type: requirement.requirement_type.trim(),
        position: asNumber(requirement.position) ?? 0,
        raw_text: requirement.raw_text.trim() || null,
      })),
      applicability: group.applicability,
      verified: group.verified,
      raw_text: group.raw_text.trim() || null,
    })),
    replacements: form.replacements.filter((replacement) => replacement.replacement_code.trim() || replacement.course_code.trim()).map((replacement) => ({
      relationship_type: replacement.relation.trim() || "replacement",
      related_course_code: (replacement.replacement_code.trim() || replacement.course_code.trim()).toUpperCase(),
      course_code: replacement.course_code.trim() || null,
      replacement_code: replacement.replacement_code.trim() || null,
      program_code: replacement.program_code.trim() || null,
      curriculum_version: replacement.curriculum_version.trim() || null,
      verified: replacement.verified,
      raw_text: replacement.raw_text.trim() || null,
    })),
  };
}

function sourceConflictItems(row: CatalogCourseRow | null, detail: CatalogCourseDetail | null): CatalogSourceConflict[] {
  if (detail?.source_conflicts?.length) return detail.source_conflicts;
  const issues = detail?.issues ?? [];
  const issueConflicts = issues
    .filter((issue) => issue.code === "source_conflict")
    .map((issue) => ({
      field: String(issue.field ?? "unknown"),
      source_value: issue.source_value,
      override_value: issue.override_value,
      source_observed_at: typeof issue.source_observed_at === "string" ? issue.source_observed_at : null,
      source_status: typeof issue.source_status === "string" ? issue.source_status : null,
      override_reason: typeof issue.override_reason === "string" ? issue.override_reason : null,
    }));
  if (issueConflicts.length) return issueConflicts;
  return row && Array.isArray(row.source_conflicts) ? row.source_conflicts : [];
}

function draftOverrideValues(detail: CatalogCourseDetail): Record<string, unknown> {
  const value = detail.draft?.field_overrides ?? detail.field_overrides;
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function importItems(data: CatalogImportsResponse | undefined): CatalogImportJob[] {
  return data?.imports ?? [];
}

function releaseItems(data: CatalogReleasesResponse | undefined): CatalogRelease[] {
  return data?.releases ?? [];
}

function releaseStatus(release: CatalogRelease): string {
  return release.active ? "active" : release.operation ?? release.status ?? "published";
}

function importProgress(job: CatalogImportJob): { processed: number; total: number; phase: string; conflicts: number; error: string | null } {
  const checkpoint = job.checkpoint && typeof job.checkpoint === "object" ? job.checkpoint : {};
  const steps = Array.isArray(checkpoint.steps) ? checkpoint.steps : [];
  const offsetValue = Number(checkpoint.offset ?? 0);
  const processed = Number.isFinite(offsetValue) ? Math.max(0, Math.trunc(offsetValue)) : 0;
  const total = steps.length || (job.status === "completed" ? processed : 0);
  const current = steps[processed];
  const phase = current && typeof current === "object" && typeof (current as Record<string, unknown>).tool === "string"
    ? String((current as Record<string, unknown>).tool)
    : job.status;
  return {
    processed: Math.min(processed, total || processed),
    total,
    phase,
    conflicts: Number(checkpoint.conflicts ?? 0) || 0,
    error: job.error_detail ?? job.error_code ?? null,
  };
}

export function CoursesPanel({
  principal,
  title,
  description,
}: {
  principal: AdminPrincipal;
  title: string;
  description: string;
}) {
  const { pick } = useLocale();
  const client = useQueryClient();
  const canRead = principal.permissions.includes("catalog:read");
  const canWrite = principal.permissions.includes("catalog:write");
  const canPublish = principal.permissions.includes("catalog:publish");
  const [view, setView] = useState<CatalogView>("courses");
  const [term, setTerm] = useState(DEFAULT_TERM);
  const [department, setDepartment] = useState("");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [stateFilter, setStateFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [selectedCodes, setSelectedCodes] = useState<Set<string>>(new Set());
  const [selectedCourse, setSelectedCourse] = useState<CatalogCourseRow | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [publishOpen, setPublishOpen] = useState(false);
  const [rollbackRelease, setRollbackRelease] = useState<CatalogRelease | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(search.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [search]);

  const changeTerm = (value: string) => { setTerm(value); setOffset(0); };
  const changeDepartment = (value: string) => { setDepartment(value); setOffset(0); };
  const changeState = (value: string) => { setStateFilter(value); setOffset(0); };
  const changeSearch = (value: string) => { setSearch(value); setOffset(0); };

  const releases = useQuery({
    queryKey: ["admin", "catalog", "releases", term],
    queryFn: () => adminGet<CatalogReleasesResponse>(`catalog/releases?term=${encodeURIComponent(term)}`),
    enabled: Boolean(term.trim()),
    refetchInterval: 15_000,
  });
  const courses = useQuery({
    queryKey: ["admin", "catalog", "courses", term, department, query, stateFilter, offset],
    queryFn: () => {
      const params = new URLSearchParams({ term, offset: String(offset), limit: String(PAGE_SIZE) });
      if (department.trim()) params.set("department", department.trim());
      if (query) params.set("query", query);
      if (stateFilter) params.set("state", stateFilter);
      return adminGet<CatalogCourseListResponse>(`catalog/courses?${params.toString()}`);
    },
    enabled: Boolean(term.trim()),
    refetchInterval: 15_000,
  });
  const detail = useQuery({
    queryKey: ["admin", "catalog", "course", term, selectedCourse?.course_code],
    queryFn: async () => {
      const response = await adminGet<CatalogCourseDetail>(`catalog/courses/${encodeURIComponent(selectedCourse!.course_code)}?term=${encodeURIComponent(term)}`);
      return normalizeCatalogDetail(response, selectedCourse!);
    },
    enabled: Boolean(term.trim() && selectedCourse?.course_code),
  });
  const imports = useQuery({
    queryKey: ["admin", "catalog", "imports", term],
    queryFn: () => adminGet<CatalogImportsResponse>(`catalog/imports?term=${encodeURIComponent(term)}&limit=50`),
    enabled: view === "imports",
    refetchInterval: view === "imports" ? 4_000 : false,
  });
  const refresh = () => {
    void client.invalidateQueries({ queryKey: ["admin", "catalog"] });
  };
  const rows = courses.data?.courses ?? [];
  const selectedRows = rows.filter((row) => selectedCodes.has(row.course_code));
  const activeReleaseId = courses.data?.release_id ?? releases.data?.active_release_id ?? null;
  const conflictTotal = selectedRows.reduce((count, row) => count + conflictCount(row.source_conflicts), 0);

  const toggleSelected = (code: string) => {
    setSelectedCodes((current) => {
      const next = new Set(current);
      if (next.has(code)) next.delete(code); else next.add(code);
      return next;
    });
  };

  const createDraft = useMutation({
    mutationFn: (payload: { course_code: string; data?: CatalogDraftPatch; reason: string }) => adminMutate<CatalogDraft>("catalog/drafts", "POST", {
      term,
      course_code: payload.course_code,
      data: payload.data,
      reason: payload.reason,
    }),
    onSuccess: (draft) => {
      toast.success(pick({ tr: `${draft.course_code} için taslak oluşturuldu.`, en: `Draft created for ${draft.course_code}.` }));
      setCreateOpen(false);
      refresh();
    },
    onError: (error) => toast.error(error.message),
  });
  const importJob = useMutation({
    mutationFn: (payload: { department: string; course_codes: string[]; reason: string }) => adminMutate<{ id: string }>("catalog/imports", "POST", {
      term,
      department: payload.department.trim() || null,
      course_codes: payload.course_codes.length ? payload.course_codes : undefined,
      reason: payload.reason,
    }),
    onSuccess: () => {
      toast.success(pick({ tr: "Katalog yenileme sıraya alındı.", en: "Catalog refresh queued." }));
      setImportOpen(false);
      setView("imports");
      refresh();
    },
    onError: (error) => toast.error(error.message),
  });
  const publish = useMutation({
    mutationFn: (payload: { reason: string; acknowledge_conflicts: boolean }) => adminMutate<CatalogOperationResponse>("catalog/publish", "POST", {
      term,
      draft_ids: selectedRows.map((row) => row.draft_id).filter((id): id is string => Boolean(id)),
      expected_release_id: activeReleaseId,
      acknowledge_conflicts: payload.acknowledge_conflicts,
      idempotency_key: crypto.randomUUID(),
      reason: payload.reason,
    }),
    onSuccess: () => {
      toast.success(pick({ tr: "Seçilen ders taslakları yayınlandı.", en: "Selected course drafts were published." }));
      setPublishOpen(false);
      setSelectedCodes(new Set());
      refresh();
    },
    onError: (error) => toast.error(error.message),
  });
  const rollback = useMutation({
    mutationFn: (payload: { target_release_id: string; reason: string }) => adminMutate<CatalogOperationResponse>("catalog/rollback", "POST", {
      term,
      target_release_id: payload.target_release_id,
      expected_release_id: activeReleaseId,
      idempotency_key: crypto.randomUUID(),
      reason: payload.reason,
    }),
    onSuccess: () => {
      toast.success(pick({ tr: "Katalog sürümü geri yüklendi.", en: "Catalog release rolled back." }));
      setRollbackRelease(null);
      refresh();
    },
    onError: (error) => toast.error(error.message),
  });

  const actions = (
    <div className="flex flex-wrap gap-2">
      <Button size="sm" variant="outline" onClick={refresh} disabled={courses.isFetching || releases.isFetching}>
        <RefreshCwIcon className={cn((courses.isFetching || releases.isFetching) && "animate-spin")} />
        {pick({ tr: "Yenile", en: "Refresh" })}
      </Button>
      {canWrite ? <Button size="sm" variant="outline" onClick={() => setImportOpen(true)}><UploadCloudIcon />{pick({ tr: "Yenileme iste", en: "Request refresh" })}</Button> : null}
      {canWrite ? <Button size="sm" onClick={() => setCreateOpen(true)}><BookOpenIcon />{pick({ tr: "Taslak oluştur", en: "Create draft" })}</Button> : null}
    </div>
  );

  return (
    <>
      <PanelHeader title={title} description={description} actions={actions} />
      <div className="space-y-5">
        <SectionNav
          value={view}
          onChange={(value) => setView(value as CatalogView)}
          items={[
            { id: "courses", label: pick({ tr: "Dersler", en: "Courses" }) },
            { id: "imports", label: pick({ tr: "Yenileme işleri", en: "Refresh jobs" }) },
            { id: "releases", label: pick({ tr: "Sürümler", en: "Releases" }) },
          ]}
        />
        {view === "courses" ? (
          <>
            <CourseFilters term={term} setTerm={changeTerm} department={department} setDepartment={changeDepartment} search={search} setSearch={changeSearch} state={stateFilter} setState={changeState} />
            <CoverageSummary data={courses.data} />
            {selectedCodes.size ? <BatchBar selectedRows={selectedRows} canWrite={canWrite} canPublish={canPublish} conflictTotal={conflictTotal} onImport={() => setImportOpen(true)} onPublish={() => setPublishOpen(true)} onClear={() => setSelectedCodes(new Set())} /> : null}
            {courses.isLoading ? <Skeleton className="h-72 rounded-xl" /> : courses.error ? <ErrorState error={courses.error} retry={() => void courses.refetch()} /> : <CourseTable rows={rows} selected={selectedCodes} onSelect={toggleSelected} onOpen={setSelectedCourse} total={courses.data?.total ?? 0} offset={offset} setOffset={setOffset} />}
            {selectedCourse ? <CourseDetailSheet row={selectedCourse} detail={detail.data} loading={detail.isLoading} error={detail.error} term={term} canRead={canRead} canWrite={canWrite} canPublish={canPublish} onClose={() => setSelectedCourse(null)} onRefresh={refresh} onCreateDraft={() => setCreateOpen(true)} /> : null}
          </>
        ) : view === "imports" ? (
          <ImportJobsPanel data={imports.data} loading={imports.isLoading} error={imports.error} canWrite={canWrite} onRefresh={() => void imports.refetch()} onRequest={() => setImportOpen(true)} />
        ) : (
          <ReleaseHistoryPanel data={releases.data} loading={releases.isLoading} error={releases.error} canWrite={canPublish} onRefresh={() => void releases.refetch()} onRollback={setRollbackRelease} />
        )}
      </div>
      {createOpen ? <CreateDraftDialog term={term} pending={createDraft.isPending} onClose={() => setCreateOpen(false)} onSubmit={(payload) => createDraft.mutate(payload)} /> : null}
      {importOpen ? <ImportDialog selectedCodes={[...selectedCodes]} term={term} pending={importJob.isPending} onClose={() => setImportOpen(false)} onSubmit={(payload) => importJob.mutate(payload)} /> : null}
      {publishOpen ? <PublishDialog selectedRows={selectedRows} conflictTotal={conflictTotal} pending={publish.isPending} onClose={() => setPublishOpen(false)} onSubmit={(payload) => publish.mutate(payload)} /> : null}
      {rollbackRelease ? <RollbackDialog release={rollbackRelease} pending={rollback.isPending} onClose={() => setRollbackRelease(null)} onSubmit={(payload) => rollback.mutate(payload)} /> : null}
    </>
  );
}

function Field({
  label,
  value,
  onChange,
  type = "text",
  placeholder,
  className,
  min,
  step,
  maxLength,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: string;
  placeholder?: string;
  className?: string;
  min?: string;
  step?: string;
  maxLength?: number;
}) {
  const generatedId = useId();
  const id = `catalog-field-${generatedId}`;
  return (
    <div className={cn("min-w-0 space-y-1.5", className)}>
      <Label htmlFor={id} className="text-xs text-muted-foreground">{label}</Label>
      <Input id={id} value={value} onChange={(event) => onChange(event.target.value)} type={type} placeholder={placeholder} min={min} step={step} maxLength={maxLength} />
    </div>
  );
}

function CourseFilters({
  term,
  setTerm,
  department,
  setDepartment,
  search,
  setSearch,
  state,
  setState,
}: {
  term: string;
  setTerm: (value: string) => void;
  department: string;
  setDepartment: (value: string) => void;
  search: string;
  setSearch: (value: string) => void;
  state: string;
  setState: (value: string) => void;
}) {
  const { pick } = useLocale();
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm"><SlidersHorizontalIcon className="size-4 text-muted-foreground" />{pick({ tr: "Katalog filtreleri", en: "Catalog filters" })}</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field label={pick({ tr: "Dönem", en: "Term" })} value={term} onChange={setTerm} placeholder="20261" />
        <Field label={pick({ tr: "Bölüm", en: "Department" })} value={department} onChange={setDepartment} placeholder="CNG" />
        <Field label={pick({ tr: "Ders ara", en: "Search courses" })} value={search} onChange={setSearch} placeholder={pick({ tr: "Kod veya başlık", en: "Code or title" })} />
        <div className="min-w-0 space-y-1.5">
          <Label htmlFor="catalog-state-filter" className="text-xs text-muted-foreground">{pick({ tr: "Durum", en: "State" })}</Label>
          <select id="catalog-state-filter" value={state} onChange={(event) => setState(event.target.value)} className="h-9 w-full rounded-lg border bg-background px-3 text-sm focus-visible:outline-2 focus-visible:outline-primary">
            <option value="">{pick({ tr: "Tümü", en: "All" })}</option>
            <option value="published">{pick({ tr: "Yayında", en: "Published" })}</option>
            <option value="draft">{pick({ tr: "Taslak", en: "Draft" })}</option>
            <option value="retired">{pick({ tr: "Arşivlenmiş", en: "Retired" })}</option>
          </select>
        </div>
      </CardContent>
    </Card>
  );
}

function CoverageSummary({ data }: { data?: CatalogCourseListResponse }) {
  const { pick, locale } = useLocale();
  const counts = data?.counts;
  const cards = [
    [pick({ tr: "Listelenen", en: "Listed" }), counts?.listed],
    [pick({ tr: "Ayrıntılı", en: "Detailed" }), counts?.detailed],
    [pick({ tr: "Ders planlayıcıya uygun", en: "Schedulable" }), counts?.schedulable],
    [pick({ tr: "Kısıtları doğrulanmış", en: "Restrictions verified" }), counts?.restrictions_verified ?? counts?.restriction_verified],
    [pick({ tr: "Ön koşulları doğrulanmış", en: "Prerequisites verified" }), counts?.prerequisites_verified ?? counts?.prerequisite_verified],
  ] as const;
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
      {cards.map(([label, value]) => (
        <Card key={label} size="sm"><CardContent className="pt-3"><p className="text-xs text-muted-foreground">{label}</p><p className="mt-1 text-2xl font-semibold tabular-nums">{value === undefined ? "—" : value.toLocaleString(locale === "tr" ? "tr-TR" : "en-US")}</p></CardContent></Card>
      ))}
      <Card size="sm" className="sm:col-span-2 lg:col-span-5"><CardContent className="flex flex-wrap items-center gap-2 pt-3 text-sm">
        <span className="text-muted-foreground">{pick({ tr: "Aktif yayın", en: "Active release" })}</span>
        {data?.release_id ? <Badge variant="secondary" className="font-mono">{data.release_id}</Badge> : <StatusBadge value="unknown" />}
        {!data?.release_id ? <span className="text-muted-foreground">{pick({ tr: "Henüz yayınlanmış bir sürüm yok.", en: "No published release yet." })}</span> : null}
      </CardContent></Card>
    </div>
  );
}

function BatchBar({
  selectedRows,
  canWrite,
  canPublish,
  conflictTotal,
  onImport,
  onPublish,
  onClear,
}: {
  selectedRows: CatalogCourseRow[];
  canWrite: boolean;
  canPublish: boolean;
  conflictTotal: number;
  onImport: () => void;
  onPublish: () => void;
  onClear: () => void;
}) {
  const { pick } = useLocale();
  const draftCount = selectedRows.filter((row) => Boolean(row.draft_id)).length;
  return (
    <Card className="border-primary/25 bg-primary/[0.03]">
      <CardContent className="flex flex-wrap items-center gap-2 pt-4">
        <span className="mr-auto text-sm font-medium">{pick({ tr: `${selectedRows.length} ders seçildi`, en: `${selectedRows.length} courses selected` })}</span>
        {conflictTotal ? <span className="flex items-center gap-1 text-sm text-amber-700 dark:text-amber-300"><ShieldAlertIcon className="size-4" />{pick({ tr: `${conflictTotal} kaynak çakışması`, en: `${conflictTotal} source conflicts` })}</span> : null}
        {canWrite ? <Button variant="outline" size="sm" onClick={onImport}><RefreshCwIcon />{pick({ tr: "Seçilenleri yenile", en: "Refresh selected" })}</Button> : null}
        {canPublish ? <Button size="sm" onClick={onPublish} disabled={!draftCount}><SendIcon />{draftCount ? pick({ tr: `${draftCount} taslağı yayınla`, en: `Publish ${draftCount} drafts` }) : pick({ tr: "Taslak yok", en: "No drafts" })}</Button> : null}
        <Button variant="ghost" size="sm" onClick={onClear}>{pick({ tr: "Temizle", en: "Clear" })}</Button>
      </CardContent>
    </Card>
  );
}

function CourseTable({
  rows,
  selected,
  onSelect,
  onOpen,
  total,
  offset,
  setOffset,
}: {
  rows: CatalogCourseRow[];
  selected: Set<string>;
  onSelect: (code: string) => void;
  onOpen: (row: CatalogCourseRow) => void;
  total: number;
  offset: number;
  setOffset: (value: number) => void;
}) {
  const { pick } = useLocale();
  const allSelected = rows.length > 0 && rows.every((row) => selected.has(row.course_code));
  const toggleAll = () => rows.forEach((row) => {
    const isSelected = selected.has(row.course_code);
    if (allSelected === isSelected) onSelect(row.course_code);
  });
  if (!rows.length) return <Card><EmptyState title={pick({ tr: "Ders bulunamadı", en: "No courses found" })} description={pick({ tr: "Dönem veya filtreleri değiştirip tekrar deneyin.", en: "Adjust the term or filters and try again." })} icon={<BookOpenIcon className="size-4" />} /></Card>;
  return (
    <Card className="overflow-hidden">
      <Table>
        <TableHeader><TableRow>
          <TableHead className="w-10"><input aria-label={pick({ tr: "Sayfadaki tüm dersleri seç", en: "Select all courses on page" })} type="checkbox" checked={allSelected} onChange={toggleAll} /></TableHead>
          <TableHead>{pick({ tr: "Ders", en: "Course" })}</TableHead>
          <TableHead>{pick({ tr: "Bölüm", en: "Department" })}</TableHead>
          <TableHead>{pick({ tr: "Kapsam", en: "Coverage" })}</TableHead>
          <TableHead>{pick({ tr: "Kaynak", en: "Source" })}</TableHead>
          <TableHead className="text-right">{pick({ tr: "İşlem", en: "Action" })}</TableHead>
        </TableRow></TableHeader>
        <TableBody>{rows.map((row) => {
          const conflicts = conflictCount(row.source_conflicts);
          return <TableRow key={row.course_code} data-state={selected.has(row.course_code) ? "selected" : undefined}>
            <TableCell><input aria-label={pick({ tr: `${row.course_code} seç`, en: `Select ${row.course_code}` })} type="checkbox" checked={selected.has(row.course_code)} onChange={() => onSelect(row.course_code)} /></TableCell>
            <TableCell><button type="button" className="text-left hover:underline" onClick={() => onOpen(row)}><span className="font-mono font-semibold">{row.course_code}</span><span className="mt-0.5 block max-w-[20rem] truncate text-xs text-muted-foreground">{row.title}</span></button></TableCell>
            <TableCell>{row.department ?? "—"}</TableCell>
            <TableCell><div className="flex flex-wrap gap-1"><StatusBadge value={completenessStatus(row.completeness)} /><span className="text-xs text-muted-foreground">{row.section_count} {pick({ tr: "şube", en: "sections" })}</span></div></TableCell>
            <TableCell><div className="flex flex-wrap gap-1"><StatusBadge value={row.state} /><StatusBadge value={row.freshness} />{conflicts ? <Badge variant="destructive">{conflicts} {pick({ tr: "çakışma", en: "conflicts" })}</Badge> : null}</div></TableCell>
            <TableCell className="text-right"><Button variant="outline" size="sm" onClick={() => onOpen(row)}><ListChecksIcon />{pick({ tr: "Aç", en: "Open" })}</Button></TableCell>
          </TableRow>;
        })}</TableBody>
      </Table>
      <div className="flex flex-wrap items-center justify-between gap-3 border-t p-3 text-sm text-muted-foreground">
        <span>{total ? `${offset + 1}–${Math.min(offset + rows.length, total)} / ${total}` : "0"}</span>
        <div className="flex gap-2"><Button variant="outline" size="sm" disabled={offset <= 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}><ChevronLeftIcon />{pick({ tr: "Önceki", en: "Previous" })}</Button><Button variant="outline" size="sm" disabled={offset + rows.length >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>{pick({ tr: "Sonraki", en: "Next" })}<ChevronRightIcon /></Button></div>
      </div>
    </Card>
  );
}

function CourseDetailSheet({
  row,
  detail,
  loading,
  error,
  term,
  canRead,
  canWrite,
  canPublish,
  onClose,
  onRefresh,
  onCreateDraft,
}: {
  row: CatalogCourseRow;
  detail?: CatalogCourseDetail;
  loading: boolean;
  error: Error | null;
  term: string;
  canRead: boolean;
  canWrite: boolean;
  canPublish: boolean;
  onClose: () => void;
  onRefresh: () => void;
  onCreateDraft: () => void;
}) {
  const { pick } = useLocale();
  return (
    <Card className="border-primary/30 shadow-sm">
      <CardHeader className="flex flex-wrap items-start justify-between gap-3 border-b">
        <div>
          <CardTitle className="flex items-center gap-2"><BookOpenIcon className="size-5 text-primary" /><span className="font-mono">{row.course_code}</span><span className="font-normal">{row.title}</span></CardTitle>
          <CardDescription>{pick({ tr: `Dönem ${term}`, en: `Term ${term}` })}</CardDescription>
        </div>
        <div className="flex gap-2"><Button variant="outline" size="sm" onClick={onRefresh} disabled={loading}><RefreshCwIcon className={cn(loading && "animate-spin")} />{pick({ tr: "Yenile", en: "Refresh" })}</Button><Button variant="ghost" size="sm" onClick={onClose}>{pick({ tr: "Kapat", en: "Close" })}</Button></div>
      </CardHeader>
      <CardContent className="pt-4">
        {loading ? <div className="space-y-3"><Skeleton className="h-8 w-1/2" /><Skeleton className="h-32" /></div> : error ? <ErrorState error={error} retry={onRefresh} /> : detail ? <CourseDetailContent key={`${detail.course_code}:${detail.draft_revision ?? detail.draft?.revision ?? "base"}`} row={row} detail={detail} canRead={canRead} canWrite={canWrite} canPublish={canPublish} onRefresh={onRefresh} onCreateDraft={onCreateDraft} /> : <EmptyState title={pick({ tr: "Ders ayrıntısı yok", en: "Course details unavailable" })} />}
      </CardContent>
    </Card>
  );
}

function CourseDetailContent({
  row,
  detail,
  canRead,
  canWrite,
  canPublish,
  onRefresh,
  onCreateDraft,
}: {
  row: CatalogCourseRow;
  detail: CatalogCourseDetail;
  canRead: boolean;
  canWrite: boolean;
  canPublish: boolean;
  onRefresh: () => void;
  onCreateDraft: () => void;
}) {
  const { pick, locale } = useLocale();
  const [tab, setTab] = useState<CourseTab>("overview");
  const [editing, setEditing] = useState(false);
  const draftDetail = draftDetailFrom(detail);
  const [form, setForm] = useState<CourseForm>(() => formFromDetail(draftDetail));
  const [reason, setReason] = useState("");
  const [verify, setVerify] = useState(false);
  const [verificationEvidence, setVerificationEvidence] = useState("");
  const draft = detail.draft ?? null;
  const draftId = row.draft_id ?? detail.draft_id ?? draft?.id ?? null;
  const expectedRevision = detail.draft_revision ?? draft?.revision ?? 0;
  const conflicts = sourceConflictItems(row, detail);
  const updateDraft = useMutation({
    mutationFn: () => {
      if (!draftId) throw new Error(pick({ tr: "Önce bir taslak oluşturun.", en: "Create a draft before editing." }));
      if (reason.trim().length < 3) throw new Error(pick({ tr: "Değişiklik nedeni en az 3 karakter olmalı.", en: "A change reason must be at least 3 characters." }));
      return adminMutate<CatalogDraft>(`catalog/drafts/${encodeURIComponent(draftId)}`, "PATCH", {
        expected_revision: expectedRevision,
        patch: formToPatch(form),
        reason: reason.trim(),
        verify,
        verification_evidence: verify ? verificationEvidence.trim() : undefined,
      });
    },
    onSuccess: () => {
      toast.success(pick({ tr: "Ders taslağı kaydedildi.", en: "Course draft saved." }));
      setEditing(false);
      setReason("");
      setVerify(false);
      setVerificationEvidence("");
      onRefresh();
    },
    // Keep the local form untouched. A 409 can be reviewed against the current
    // server detail without erasing what the editor has typed.
    onError: (error) => toast.error(error.message),
  });
  const startEditing = () => {
    if (!draftId) {
      onCreateDraft();
      return;
    }
    setEditing(true);
  };
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge value={detail.state} /><StatusBadge value={completenessStatus(detail.completeness)} />
        {detail._catalog?.release_id ? <Badge variant="outline" className="font-mono">{detail._catalog.release_id}</Badge> : null}
        {conflicts.length ? <Badge variant="destructive" className="gap-1"><ShieldAlertIcon />{conflicts.length} {pick({ tr: "kaynak çakışması", en: "source conflicts" })}</Badge> : null}
        <span className="ml-auto flex gap-2">{canWrite ? <Button size="sm" variant={editing ? "secondary" : "outline"} onClick={startEditing} disabled={updateDraft.isPending}><BookOpenIcon />{draftId ? pick({ tr: editing ? "Düzenleniyor" : "Düzenle", en: editing ? "Editing" : "Edit" }) : pick({ tr: "Taslak oluştur", en: "Create draft" })}</Button> : null}{canPublish && draftId ? <Badge variant="secondary">{pick({ tr: "Yayın yetkisi var", en: "Can publish" })}</Badge> : null}</span>
      </div>
      {editing ? <CourseEditForm form={form} setForm={setForm} reason={reason} setReason={setReason} verify={verify} setVerify={setVerify} verificationEvidence={verificationEvidence} setVerificationEvidence={setVerificationEvidence} pending={updateDraft.isPending} onCancel={() => setEditing(false)} onSave={() => updateDraft.mutate()} /> : null}
      <SectionNav value={tab} onChange={(value) => setTab(value as CourseTab)} items={[
        { id: "overview", label: pick({ tr: "Genel bakış", en: "Overview" }) },
        { id: "sections", label: pick({ tr: "Şubeler / toplantılar", en: "Sections / meetings" }) },
        { id: "rules", label: pick({ tr: "Kurallar", en: "Rules" }) },
        { id: "sources", label: pick({ tr: "Kaynaklar / farklar", en: "Sources / diff" }) },
        { id: "history", label: pick({ tr: "Geçmiş", en: "History" }) },
      ]} />
      {tab === "overview" ? <OverviewView detail={draftDetail} locale={locale} /> : null}
      {tab === "sections" ? <SectionsView sections={draftDetail.sections} /> : null}
      {tab === "rules" ? <RulesView detail={draftDetail} /> : null}
      {tab === "sources" ? <SourcesView detail={detail} row={row} canRead={canRead} canWrite={canWrite} /> : null}
      {tab === "history" ? <HistoryView entries={detail.history} locale={locale} /> : null}
    </div>
  );
}

function OverviewView({ detail, locale }: { detail: CatalogCourseDetail; locale: "tr" | "en" }) {
  const { pick } = useLocale();
  const items = [
    [pick({ tr: "Ders kodu", en: "Course code" }), detail.course_code],
    [pick({ tr: "Başlık", en: "Title" }), detail.title],
    [pick({ tr: "Bölüm", en: "Department" }), detail.department ?? "—"],
    [pick({ tr: "Yerel kredi", en: "Local credits" }), detail.local_credits ?? detail.credits ?? "—"],
    [pick({ tr: "ECTS", en: "ECTS" }), detail.ects ?? "—"],
    [pick({ tr: "Seviye", en: "Level" }), detail.level ?? "—"],
    [pick({ tr: "Açılma durumu", en: "Availability" }), detail.availability ?? "—"],
    [pick({ tr: "Şube sayısı", en: "Sections" }), detail.sections.length],
  ];
  return <div className="space-y-4"><div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">{items.map(([label, value]) => <div className="rounded-xl border bg-muted/25 p-3" key={label}><p className="text-xs text-muted-foreground">{label}</p><p className="mt-1 break-words font-medium">{typeof value === "number" ? value.toLocaleString(locale === "tr" ? "tr-TR" : "en-US") : value}</p></div>)}</div><Card size="sm"><CardHeader><CardTitle className="text-sm">{pick({ tr: "Veri durumu", en: "Data state" })}</CardTitle></CardHeader><CardContent className="grid gap-2 text-sm sm:grid-cols-2"><StateNote label={pick({ tr: "Tamlık", en: "Completeness" })} value={completenessStatus(detail.completeness)} /><StateNote label={pick({ tr: "Yayın", en: "Publication" })} value={detail.state} /><StateNote label={pick({ tr: "Kaynak sürümü", en: "Source release" })} value={detail._catalog?.release_id ?? null} /><StateNote label={pick({ tr: "Ön koşul grupları", en: "Prerequisite groups" })} value={detail.prerequisite_groups.length} /></CardContent></Card></div>;
}

function StateNote({ label, value }: { label: string; value: unknown }) {
  return <div className="flex items-center justify-between gap-3 rounded-lg border px-3 py-2"><span className="text-muted-foreground">{label}</span><span className="font-medium">{typeof value === "number" ? value : value ? <StatusBadge value={String(value)} /> : "—"}</span></div>;
}

function SectionsView({ sections }: { sections: CatalogSection[] }) {
  const { pick } = useLocale();
  if (!sections.length) return <EmptyState title={pick({ tr: "Şube bilgisi yok", en: "No section data" })} />;
  return (
    <div className="space-y-3">
      {sections.map((section, sectionIndex) => {
        const meetingStatus = normalizeMeetingStatus(section.meetings_status);
        const meetingCopy = meetingStatusCopy(meetingStatus);
        return (
          <Card size="sm" key={section.id ?? `${section.section_code}-${sectionIndex}`}>
            <CardHeader className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <CardTitle className="text-sm">
                  <span className="font-mono">{section.section_code}</span>
                  {section.notes ? <span className="ml-2 font-normal text-muted-foreground">{section.notes}</span> : null}
                </CardTitle>
                <CardDescription className="flex flex-wrap items-center gap-2">
                  <StatusBadge value={section.status} />
                  <StatusBadge value={meetingStatus} />
                  <span className="text-xs">{pick(meetingCopy.label)}</span>
                </CardDescription>
              </div>
              <span className="text-xs text-muted-foreground">
                {section.meetings.length} {pick({ tr: "toplantı", en: "meetings" })}
              </span>
            </CardHeader>
            <CardContent className="grid gap-4 lg:grid-cols-3">
              <div>
                <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{pick({ tr: "Toplantılar", en: "Meetings" })}</h4>
                {section.meetings.length ? (
                  <ul className="space-y-1 text-sm">
                    {section.meetings.map((meeting, index) => (
                      <li key={`${meeting.weekday}-${meeting.start_minute}-${index}`} className="rounded-lg border px-3 py-2">
                        <span>{meetingLabel(meeting, pick)}</span> <StatusBadge value={meeting.status} />
                      </li>
                    ))}
                  </ul>
                ) : (
                  <div className="rounded-lg border border-dashed bg-muted/20 p-3">
                    <p className="text-sm font-medium">{pick(meetingCopy.label)}</p>
                    <p className="mt-1 text-xs text-muted-foreground">{pick(meetingCopy.empty)}</p>
                  </div>
                )}
              </div>
              <div>
                <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{pick({ tr: "Eğitmenler", en: "Instructors" })}</h4>
                {section.instructors.length ? (
                  <ul className="space-y-1 text-sm">
                    {section.instructors.map((instructor, index) => (
                      <li key={`${instructor.source_name ?? instructor.name ?? "instructor"}-${index}`} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border px-3 py-2">
                        <span>{instructor.source_name ?? instructor.name ?? "—"}{instructor.position ? <span className="ml-1 text-muted-foreground">· {instructor.position}</span> : null}</span>
                        <StatusBadge value={instructor.resolution_status} />
                      </li>
                    ))}
                  </ul>
                ) : <p className="text-sm text-muted-foreground">{pick({ tr: "Bilinmiyor", en: "Unknown" })}</p>}
              </div>
              <div>
                <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{pick({ tr: "Kısıtlar", en: "Restrictions" })}</h4>
                {section.restrictions.length ? (
                  <ul className="space-y-1 text-sm">
                    {section.restrictions.map((restriction, index) => (
                      <li key={`${restriction.kind}-${index}`} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border px-3 py-2">
                        <span>{restriction.kind}: {restriction.value_text ?? restriction.value_numeric ?? restriction.program_code ?? "—"}</span>
                        <StatusBadge value={restriction.verified ? "verified" : "unverified"} />
                      </li>
                    ))}
                  </ul>
                ) : <p className="text-sm text-muted-foreground">{pick({ tr: "Kısıt bilgisi yok", en: "No restriction data" })}</p>}
              </div>
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}

function RulesView({ detail }: { detail: CatalogCourseDetail }) {
  const { pick } = useLocale();
  return <div className="grid gap-3 lg:grid-cols-2"><Card size="sm"><CardHeader><CardTitle className="text-sm">{pick({ tr: "Ön koşullar", en: "Prerequisites" })}</CardTitle></CardHeader><CardContent className="space-y-3">{detail.prerequisite_groups.length ? detail.prerequisite_groups.map((group, index) => <div className="rounded-lg border p-3" key={`${group.group_no}-${index}`}><div className="flex flex-wrap items-center gap-2 text-sm font-medium"><Badge variant="outline">#{group.group_no}</Badge><StatusBadge value={group.logic} />{group.program_code ? <span className="font-mono text-xs">{group.program_code}</span> : null}</div><ul className="mt-2 space-y-1 text-sm text-muted-foreground">{group.requirements.map((requirement, requirementIndex) => <li key={`${requirement.course_code}-${requirementIndex}`}><span className="font-mono text-foreground">{requirement.course_code}</span>{requirement.minimum_grade ? ` · ${requirement.minimum_grade}` : ""}{requirement.requirement_type ? ` · ${requirement.requirement_type}` : ""}</li>)}</ul></div>) : <p className="text-sm text-muted-foreground">{pick({ tr: "Ön koşul bilgisi yok.", en: "No prerequisite information." })}</p>}</CardContent></Card><Card size="sm"><CardHeader><CardTitle className="text-sm">{pick({ tr: "Yerine geçen dersler", en: "Replacements" })}</CardTitle></CardHeader><CardContent>{detail.replacements.length ? <ul className="space-y-2 text-sm">{detail.replacements.map((replacement, index) => <li className="rounded-lg border px-3 py-2" key={`${replacement.course_code}-${replacement.replacement_code}-${index}`}><span className="font-mono">{replacement.course_code ?? "—"}</span><span className="mx-2 text-muted-foreground">→</span><span className="font-mono">{replacement.replacement_code ?? replacement.equivalent_code ?? "—"}</span>{replacement.relation ? <span className="ml-2 text-muted-foreground">({replacement.relation})</span> : null}</li>)}</ul> : <p className="text-sm text-muted-foreground">{pick({ tr: "Yerine geçen ders bilgisi yok.", en: "No replacement information." })}</p>}</CardContent></Card></div>;
}

function SourcesView({ detail, row, canRead, canWrite }: { detail: CatalogCourseDetail; row: CatalogCourseRow; canRead: boolean; canWrite: boolean }) {
  const { pick, locale } = useLocale();
  const conflicts = sourceConflictItems(row, detail);
  return (
    <div className="space-y-3">
      <Card size="sm">
        <CardHeader>
          <CardTitle className="text-sm">{pick({ tr: "Kaynak gözlemleri", en: "Source observations" })}</CardTitle>
          <CardDescription>{pick({ tr: "Kaynak aracı, durum ve gözlem zamanları yayın öncesi inceleme için gösterilir.", en: "The source tool, status, and observation times are shown for pre-publish review." })}</CardDescription>
        </CardHeader>
        <CardContent>
          {detail.source_observations.length ? (
            <div className="space-y-2">
              {detail.source_observations.map((observation, index) => {
                const tool = observation.tool ?? observation.component ?? "—";
                const status = observation.status ?? observation.source_status ?? "unknown";
                const issues = observationIssues(observation);
                const location = [observation.department, observation.course_code, observation.section_code].filter(Boolean).join(" · ");
                return (
                  <div className="flex flex-wrap items-start justify-between gap-3 rounded-lg border px-3 py-2 text-sm" key={`${observation.id ?? tool}-${index}`}>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="font-medium">{tool}</p>
                        <StatusBadge value={status} />
                        {issues.length ? <Badge variant="outline" className="text-amber-700 dark:text-amber-300">{issues.length} {pick({ tr: "sorun", en: "issues" })}</Badge> : null}
                      </div>
                      {location ? <p className="mt-1 text-xs text-muted-foreground">{location}</p> : null}
                      <div className="mt-1 grid gap-x-4 gap-y-0.5 text-xs text-muted-foreground sm:grid-cols-2">
                        <span>{pick({ tr: "Gözlemlendi", en: "Observed" })}: {formatDate(observation.observed_at, locale)}</span>
                        <span>{pick({ tr: "Kaynak alındı", en: "Source fetched" })}: {formatDate(observation.source_fetched_at ?? observation.fetched_at, locale)}</span>
                      </div>
                      {observation.parser_version ? <p className="mt-1 text-xs text-muted-foreground">{pick({ tr: "Ayrıştırıcı", en: "Parser" })}: {observation.parser_version}</p> : null}
                      {issues.length ? <p className="mt-1 break-words text-xs text-amber-700 dark:text-amber-300">{observationIssueLabel(issues[0])}{issues.length > 1 ? ` · +${issues.length - 1}` : ""}</p> : null}
                    </div>
                    <ObservationInspector observation={observation} canRead={canRead} />
                  </div>
                );
              })}
            </div>
          ) : <EmptyState title={pick({ tr: "Kaynak gözlemi yok", en: "No source observations" })} />}
        </CardContent>
      </Card>
      <Card size="sm" className={conflicts.length ? "border-amber-500/40" : undefined}>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm">{conflicts.length ? <ShieldAlertIcon className="size-4 text-amber-600" /> : <CheckCircle2Icon className="size-4 text-emerald-600" />}{pick({ tr: "Kaynak farkları", en: "Source differences" })}</CardTitle>
          <CardDescription>{conflicts.length ? pick({ tr: "Yayınlamadan önce her farkı inceleyin ve açıklamasını kayda geçirin.", en: "Review each difference and record its reason before publishing." }) : pick({ tr: "İncelenecek bir kaynak farkı yok.", en: "There are no source differences to review." })}</CardDescription>
        </CardHeader>
        <CardContent>
          {conflicts.length ? <div className="space-y-2">{conflicts.map((conflict, index) => <div key={`${conflict.field}-${index}`} className="grid gap-2 rounded-lg border p-3 text-sm md:grid-cols-[1fr_1fr_1fr]"><div><p className="text-xs text-muted-foreground">{pick({ tr: "Alan", en: "Field" })}</p><p className="font-mono">{conflict.field}</p></div><div><p className="text-xs text-muted-foreground">{pick({ tr: "Kaynak değeri", en: "Source value" })}</p><p className="break-words">{valueLabel(conflict.source_value)}</p></div><div><p className="text-xs text-muted-foreground">{pick({ tr: "Taslak değeri", en: "Draft value" })}</p><p className="break-words">{valueLabel(conflict.override_value)}</p></div>{conflict.override_reason ? <p className="text-xs text-muted-foreground md:col-span-3">{conflict.override_reason}</p> : null}</div>)}</div> : null}
        </CardContent>
      </Card>
      <DraftOverridesPanel detail={detail} row={row} canWrite={canWrite} />
    </div>
  );
}

function observationIssues(observation: CatalogSourceObservation): unknown[] {
  if (Array.isArray(observation.issues)) return observation.issues;
  return observation.issue ? [observation.issue] : [];
}

function observationIssueLabel(value: unknown): string {
  if (typeof value === "string") return value;
  if (value && typeof value === "object") {
    const message = (value as Record<string, unknown>).message;
    if (typeof message === "string") return message;
  }
  return valueLabel(value);
}

function jsonValue(value: unknown): string {
  if (value === undefined) return "—";
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

function JsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="min-w-0">
      <p className="mb-1 text-xs font-medium text-muted-foreground">{label}</p>
      <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-muted/50 p-3 text-xs">{jsonValue(value)}</pre>
    </div>
  );
}

function ObservationInspector({ observation, canRead }: { observation: CatalogSourceObservation; canRead: boolean }) {
  const { pick, locale } = useLocale();
  const [open, setOpen] = useState(false);
  const observationId = observation.id;
  const detail = useQuery({
    queryKey: ["admin", "catalog", "observation", observationId],
    queryFn: () => adminGet<CatalogSourceObservationDetail>(`catalog/observations/${encodeURIComponent(observationId!)}`),
    enabled: open && canRead && Boolean(observationId),
  });

  if (!observationId || !canRead) return null;
  return (
    <div className="w-full shrink-0 sm:w-auto">
      <Button type="button" variant="outline" size="sm" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
        <ChevronRightIcon className={cn("transition-transform", open && "rotate-90")} />
        {pick({ tr: open ? "Ayrıntıları kapat" : "Ayrıntıları aç", en: open ? "Close inspector" : "Inspect observation" })}
      </Button>
      {open ? (
        <div className="mt-2 w-full rounded-lg border bg-muted/10 p-3 sm:min-w-[32rem] sm:max-w-3xl">
          {detail.isLoading ? <Skeleton className="h-24" /> : detail.error ? <p className="text-xs text-destructive">{detail.error.message}</p> : detail.data ? (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
                <span>{pick({ tr: "Gözlemlendi", en: "Observed" })}: {formatDate(detail.data.observed_at, locale)}</span>
                <span>{pick({ tr: "Kaynak alındı", en: "Source fetched" })}: {formatDate(detail.data.source_fetched_at, locale)}</span>
                <span>{pick({ tr: "Ayrıştırıcı", en: "Parser" })}: {detail.data.parser_version}</span>
              </div>
              <div className="grid gap-3 lg:grid-cols-3">
                <JsonBlock label={pick({ tr: "Argümanlar", en: "Arguments" })} value={detail.data.arguments} />
                <JsonBlock label={pick({ tr: "Ham yük", en: "Raw payload" })} value={detail.data.payload} />
                <JsonBlock label={pick({ tr: "Ayrıştırılmış aday", en: "Parsed candidate" })} value={detail.data.candidate_data} />
              </div>
              {detail.data.issues.length ? <JsonBlock label={pick({ tr: "Ayrıştırma sorunları", en: "Parser issues" })} value={detail.data.issues} /> : null}
            </div>
          ) : <p className="text-xs text-muted-foreground">{pick({ tr: "Gözlem ayrıntısı yok.", en: "Observation details unavailable." })}</p>}
        </div>
      ) : null}
    </div>
  );
}

function DraftOverridesPanel({ detail, row, canWrite }: { detail: CatalogCourseDetail; row: CatalogCourseRow; canWrite: boolean }) {
  const { pick } = useLocale();
  const client = useQueryClient();
  const overrideValues = draftOverrideValues(detail);
  const overrideFields = Object.keys(overrideValues).sort();
  const draftId = row.draft_id ?? detail.draft_id ?? detail.draft?.id ?? null;
  const expectedRevision = detail.draft_revision ?? detail.draft?.revision ?? null;
  const [selectedFields, setSelectedFields] = useState<Set<string>>(new Set());
  const [reason, setReason] = useState("");
  const selectedOverrideFields = overrideFields.filter((field) => selectedFields.has(field));

  const removeOverrides = useMutation({
    mutationFn: () => {
      const fields = selectedOverrideFields;
      if (!draftId) throw new Error(pick({ tr: "Önce bir taslak oluşturun.", en: "Create a draft before removing overrides." }));
      if (expectedRevision === null) throw new Error(pick({ tr: "Taslak sürümü bulunamadı.", en: "The draft revision is unavailable." }));
      if (!fields.length) throw new Error(pick({ tr: "Kaldırılacak alanları seçin.", en: "Select at least one override to remove." }));
      if (reason.trim().length < 3) throw new Error(pick({ tr: "Gerekçe en az 3 karakter olmalı.", en: "A reason must be at least 3 characters." }));
      return adminMutate<CatalogDraft>(`catalog/drafts/${encodeURIComponent(draftId)}/remove-overrides`, "POST", {
        expected_revision: expectedRevision,
        fields,
        reason: reason.trim(),
      });
    },
    onSuccess: () => {
      toast.success(pick({ tr: "Seçilen taslak düzeltmeleri kaldırıldı; kaynak değerleri taslağa geri döndü.", en: "Selected draft overrides were removed; source values are restored in the draft." }));
      setSelectedFields(new Set());
      setReason("");
      void client.invalidateQueries({ queryKey: ["admin", "catalog"] });
    },
    // Keep the selection and reason intact on conflicts so the operator can
    // compare the refreshed revision and retry without retyping the change.
    onError: (error) => toast.error(error.message),
  });

  return (
    <Card size="sm" className={overrideFields.length ? "border-amber-500/40" : undefined}>
      <CardHeader>
        <CardTitle className="text-sm">{pick({ tr: "Taslak düzeltmeleri", en: "Draft overrides" })}</CardTitle>
        <CardDescription>{pick({ tr: "Bu değerler kaynak gözlemlerinin üzerine yazılır ve yalnızca taslakta tutulur.", en: "These values override source observations and are retained in the draft only." })}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {overrideFields.length ? (
          <div className="space-y-2">
            {overrideFields.map((field) => (
              <label key={field} className="flex items-start gap-3 rounded-lg border px-3 py-2 text-sm">
                <input
                  type="checkbox"
                  checked={selectedFields.has(field)}
                  onChange={() => setSelectedFields((current) => {
                    const next = new Set(current);
                    if (next.has(field)) next.delete(field); else next.add(field);
                    return next;
                  })}
                  disabled={!canWrite || removeOverrides.isPending}
                  aria-label={pick({ tr: `${field} düzeltmesini seç`, en: `Select ${field} override` })}
                  className="mt-0.5"
                />
                <span className="min-w-0 flex-1">
                  <span className="block break-words font-mono">{field}</span>
                  <span className="mt-1 block break-words text-xs text-muted-foreground">{valueLabel(overrideValues[field])}</span>
                </span>
              </label>
            ))}
          </div>
        ) : <p className="text-sm text-muted-foreground">{pick({ tr: "Bu taslakta kayıtlı düzeltme yok.", en: "This draft has no recorded overrides." })}</p>}
        {overrideFields.length && canWrite ? (
          <div className="space-y-2 border-t pt-3">
            <Label htmlFor="catalog-override-removal-reason" className="text-xs text-muted-foreground">{pick({ tr: "Kaldırma gerekçesi", en: "Removal reason" })}</Label>
            <Textarea
              id="catalog-override-removal-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder={pick({ tr: "Kaynak değerine dönme nedeninizi yazın", en: "Explain why the source value should be restored" })}
              disabled={removeOverrides.isPending}
            />
            <p className="text-xs text-muted-foreground">{pick({ tr: "Seçilen düzeltmeler taslaktan kaldırılır; değişikliklerin yayına girmesi için ayrıca yayınlayın. En az 3 karakter.", en: "Selected overrides are removed from the draft; publish separately for the change to take effect. At least 3 characters." })}</p>
            <div className="flex justify-end">
              <Button
                variant="destructive"
                disabled={!selectedOverrideFields.length || reason.trim().length < 3 || !draftId || expectedRevision === null || removeOverrides.isPending}
                onClick={() => removeOverrides.mutate()}
              >
                {removeOverrides.isPending ? <Loader2Icon className="animate-spin" /> : <RotateCcwIcon />}
                {pick({ tr: "Seçilen düzeltmeleri kaldır", en: "Remove selected overrides" })}
              </Button>
            </div>
          </div>
        ) : null}
        {!canWrite && overrideFields.length ? <p className="text-xs text-muted-foreground">{pick({ tr: "Düzeltmeleri kaldırmak için katalog yazma yetkisi gerekir.", en: "Catalog write permission is required to remove overrides." })}</p> : null}
      </CardContent>
    </Card>
  );
}

function HistoryView({ entries, locale }: { entries: CatalogHistoryEntry[]; locale: "tr" | "en" }) {
  const { pick } = useLocale();
  if (!entries.length) return <EmptyState title={pick({ tr: "Geçmiş kaydı yok", en: "No history yet" })} />;
  return <div className="space-y-2">{entries.map((entry, index) => <div className="flex flex-wrap items-start gap-3 rounded-xl border p-3" key={`${entry.id ?? entry.action}-${index}`}><span className="grid size-8 shrink-0 place-items-center rounded-lg bg-muted"><HistoryIcon className="size-4" /></span><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><StatusBadge value={entry.action} />{entry.revision !== null && entry.revision !== undefined ? <Badge variant="outline">r{entry.revision}</Badge> : null}<span className="text-xs text-muted-foreground">{formatDate(entry.created_at, locale)}</span></div><p className="mt-1 text-sm">{entry.reason ?? pick({ tr: "Neden belirtilmedi", en: "No reason provided" })}</p>{entry.actor ? <p className="text-xs text-muted-foreground">{entry.actor}</p> : null}</div></div>)}</div>;
}

function updateAt<T>(items: T[], index: number, patch: Partial<T>): T[] {
  return items.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item);
}

const emptyMeeting = (): FormMeeting => ({ weekday: "0", start_minute: "", end_minute: "", room: "", status: "unknown", raw_label: "" });
const emptyInstructor = (): FormInstructor => ({ name: "", position: "", researcher_id: "", resolution_status: "unknown" });
const emptyRestriction = (): FormRestriction => ({ kind: "", restriction_group: "", row_index: "", value_text: "", value_numeric: "", operator: "", minimum_grade: "", given_department: "", start_char: "", end_char: "", min_cgpa: "", max_cgpa: "", min_year: "", max_year: "", start_grade: "", end_grade: "", prior_course_code: "", program_code: "", curriculum_version: "", raw_text: "", verified: false, status: "unknown" });
const emptySection = (): FormSection => ({ id: "", section_code: "", status: "draft", notes: "", syllabus_url: "", syllabus_available: null, meetings_status: "unknown", meetings: [], instructors: [], restrictions: [] });
const emptyRequirement = (): FormRequirement => ({ course_code: "", minimum_grade: "", requirement_type: "course", position: "", raw_text: "" });
const emptyGroup = (): FormPrerequisiteGroup => ({ group_no: "1", logic: "and", program_code: "", curriculum_version: "", applicability: {}, verified: false, raw_text: "", requirements: [emptyRequirement()] });
const emptyReplacement = (): FormReplacement => ({ course_code: "", replacement_code: "", relation: "", program_code: "", curriculum_version: "", verified: false, raw_text: "" });

function CourseEditForm({
  form,
  setForm,
  reason,
  setReason,
  verify,
  setVerify,
  verificationEvidence,
  setVerificationEvidence,
  pending,
  onCancel,
  onSave,
}: {
  form: CourseForm;
  setForm: Dispatch<SetStateAction<CourseForm>>;
  reason: string;
  setReason: (value: string) => void;
  verify: boolean;
  setVerify: (value: boolean) => void;
  verificationEvidence: string;
  setVerificationEvidence: (value: string) => void;
  pending: boolean;
  onCancel: () => void;
  onSave: () => void;
}) {
  const { pick } = useLocale();
  const valid = form.title.trim().length > 0 && reason.trim().length >= 3 && form.sections.every((section) => section.section_code.trim().length > 0) && (!verify || verificationEvidence.trim().length >= 3);
  return <Card className="border-primary/30 bg-primary/[0.02]"><CardHeader><CardTitle className="flex items-center gap-2 text-sm"><BookOpenIcon className="size-4" />{pick({ tr: "Taslağı düzenle", en: "Edit draft" })}</CardTitle><CardDescription>{pick({ tr: "Alanlar türlerine göre düzenlenir; kaydetme mevcut sürüme karşı iyimserlik kontrolü yapar.", en: "Fields are edited by type; saving checks the draft against its current revision." })}</CardDescription></CardHeader><CardContent className="space-y-5"><div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4"><Field label={pick({ tr: "Başlık", en: "Title" })} value={form.title} onChange={(value) => setForm((current) => ({ ...current, title: value }))} className="sm:col-span-2" /><Field label={pick({ tr: "Bölüm", en: "Department" })} value={form.department} onChange={(value) => setForm((current) => ({ ...current, department: value }))} /><Field label={pick({ tr: "Kredi", en: "Credits" })} type="number" step="0.5" value={form.credits} onChange={(value) => setForm((current) => ({ ...current, credits: value }))} /><Field label="ECTS" type="number" step="0.5" value={form.ects} onChange={(value) => setForm((current) => ({ ...current, ects: value }))} /><Field label={pick({ tr: "Seviye", en: "Level" })} value={form.level} onChange={(value) => setForm((current) => ({ ...current, level: value }))} /><Field label={pick({ tr: "Açılma durumu", en: "Availability" })} value={form.availability} onChange={(value) => setForm((current) => ({ ...current, availability: value }))} /><Field label={pick({ tr: "Kampüs", en: "Campus" })} value={form.campus} onChange={(value) => setForm((current) => ({ ...current, campus: value }))} /><label className="flex items-center gap-2 self-end pb-1 text-sm"><input type="checkbox" checked={form.is_thesis} onChange={(event) => setForm((current) => ({ ...current, is_thesis: event.target.checked }))} />{pick({ tr: "Tez dersi", en: "Thesis course" })}</label></div><SectionEditors sections={form.sections} onChange={(sections) => setForm((current) => ({ ...current, sections }))} /><RulesEditor groups={form.prerequisite_groups} replacements={form.replacements} onGroupsChange={(prerequisite_groups) => setForm((current) => ({ ...current, prerequisite_groups }))} onReplacementsChange={(replacements) => setForm((current) => ({ ...current, replacements }))} /><div className="space-y-1.5"><Label htmlFor="catalog-edit-reason" className="text-xs text-muted-foreground">{pick({ tr: "Değişiklik nedeni", en: "Change reason" })}</Label><Textarea id="catalog-edit-reason" value={reason} onChange={(event) => setReason(event.target.value)} placeholder={pick({ tr: "Bu düzeltmeyi neden yaptığınızı yazın", en: "Explain why this correction is needed" })} /><p className="text-xs text-muted-foreground">{pick({ tr: "En az 3 karakter.", en: "At least 3 characters." })}</p></div><label className="flex items-start gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/[0.04] p-3 text-sm"><input type="checkbox" checked={verify} onChange={(event) => setVerify(event.target.checked)} className="mt-0.5" /><span><strong>{pick({ tr: "Bu düzeltmeyi doğrula", en: "Verify this correction" })}</strong><span className="mt-1 block text-xs text-muted-foreground">{pick({ tr: "Kaynak kontrolü veya onaylı kayıt kanıtı girildiğinde planlayıcı bu alanı doğrulanmış sayabilir.", en: "The planner can treat this field as verified after source or approved-record evidence is recorded." })}</span></span></label>{verify ? <div className="space-y-1.5"><Label htmlFor="catalog-verification-evidence" className="text-xs text-muted-foreground">{pick({ tr: "Doğrulama kanıtı", en: "Verification evidence" })}</Label><Textarea id="catalog-verification-evidence" value={verificationEvidence} onChange={(event) => setVerificationEvidence(event.target.value)} placeholder={pick({ tr: "Kaynak URL’si, kayıt adı veya inceleme notu", en: "Source URL, record name, or review note" })} /><p className="text-xs text-muted-foreground">{pick({ tr: "En az 3 karakter.", en: "At least 3 characters." })}</p></div> : null}<div className="flex flex-wrap justify-end gap-2 border-t pt-4"><Button variant="outline" onClick={onCancel} disabled={pending}>{pick({ tr: "Vazgeç", en: "Cancel" })}</Button><Button onClick={onSave} disabled={!valid || pending}>{pending ? <Loader2Icon className="animate-spin" /> : <SaveIcon />}{pick({ tr: "Taslağı kaydet", en: "Save draft" })}</Button></div></CardContent></Card>;
}

function SectionEditors({ sections, onChange }: { sections: FormSection[]; onChange: (sections: FormSection[]) => void }) {
  const { pick } = useLocale();
  return <Card size="sm"><CardHeader className="flex flex-row items-center justify-between gap-2"><div><CardTitle className="text-sm">{pick({ tr: "Şubeler ve toplantılar", en: "Sections and meetings" })}</CardTitle><CardDescription>{pick({ tr: "Eksik zaman veya doğrulanmamış alanları açıkça bırakabilirsiniz.", en: "Unknown times and unverified fields remain explicit." })}</CardDescription></div><Button variant="outline" size="sm" onClick={() => onChange([...sections, emptySection()])}><BookOpenIcon />{pick({ tr: "Şube ekle", en: "Add section" })}</Button></CardHeader><CardContent className="space-y-3">{sections.length ? sections.map((section, index) => <SectionEditor key={`${section.id}-${index}`} section={section} onChange={(next) => onChange(updateAt(sections, index, next))} onRemove={() => onChange(sections.filter((_, itemIndex) => itemIndex !== index))} />) : <p className="text-sm text-muted-foreground">{pick({ tr: "Şube yok", en: "No sections" })}</p>}</CardContent></Card>;
}

function SectionEditor({ section, onChange, onRemove }: { section: FormSection; onChange: (section: FormSection) => void; onRemove: () => void }) {
  const { pick } = useLocale();
  const updateRestriction = (index: number, patch: Partial<FormRestriction>) => onChange({
    ...section,
    restrictions: updateAt(section.restrictions, index, patch),
  });

  return (
    <div className="space-y-3 rounded-xl border p-3">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
        <Field label={pick({ tr: "Şube kodu", en: "Section code" })} value={section.section_code} onChange={(section_code) => onChange({ ...section, section_code })} />
        <Field label={pick({ tr: "Durum", en: "Status" })} value={section.status} onChange={(status) => onChange({ ...section, status })} />
        <div className="space-y-1.5">
          <Label className="text-xs text-muted-foreground">{pick({ tr: "Toplantı durumu", en: "Meeting status" })}</Label>
          <select
            aria-label={pick({ tr: "Toplantı durumu", en: "Meeting status" })}
            className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
            value={normalizeMeetingStatus(section.meetings_status)}
            onChange={(event) => onChange({ ...section, meetings_status: normalizeMeetingStatus(event.target.value) })}
          >
            <option value="verified">{pick({ tr: "Doğrulanmış", en: "Verified" })}</option>
            <option value="untimed">{pick({ tr: "Planlanmış toplantı yok", en: "No scheduled meetings (untimed)" })}</option>
            <option value="unknown">{pick({ tr: "Bilinmiyor", en: "Unknown" })}</option>
            <option value="unpublished">{pick({ tr: "Yayımlanmadı", en: "Unpublished" })}</option>
            <option value="invalid">{pick({ tr: "Geçersiz", en: "Invalid" })}</option>
          </select>
          {normalizeMeetingStatus(section.meetings_status) === "untimed" ? <p className="text-xs text-muted-foreground">{pick({ tr: "Bu değer, zaman bilgisinin eksik olduğunu değil, açıkça toplantı olmadığını belirtir.", en: "This means the section explicitly has no scheduled meetings; it does not mean the schedule is missing." })}</p> : null}
        </div>
        <Field label={pick({ tr: "Müfredat URL'si", en: "Syllabus URL" })} value={section.syllabus_url} onChange={(syllabus_url) => onChange({ ...section, syllabus_url })} className="sm:col-span-2" />
        <div className="space-y-1.5">
          <Label className="text-xs text-muted-foreground">{pick({ tr: "Müfredat durumu", en: "Syllabus availability" })}</Label>
          <select
            aria-label={pick({ tr: "Müfredat durumu", en: "Syllabus availability" })}
            className="h-9 w-full rounded-lg border bg-background px-3 text-sm"
            value={section.syllabus_available === null || section.syllabus_available === undefined ? "" : section.syllabus_available ? "true" : "false"}
            onChange={(event) => onChange({
              ...section,
              syllabus_available: event.target.value === "" ? null : event.target.value === "true",
            })}
          >
            <option value="">{pick({ tr: "Bilinmiyor", en: "Unknown" })}</option>
            <option value="true">{pick({ tr: "Mevcut", en: "Available" })}</option>
            <option value="false">{pick({ tr: "Mevcut değil", en: "Unavailable" })}</option>
          </select>
        </div>
        <div className="sm:col-span-2 flex items-end justify-end lg:col-span-1">
          <Button variant="destructive" size="sm" onClick={onRemove}><RotateCcwIcon />{pick({ tr: "Şubeyi kaldır", en: "Remove section" })}</Button>
        </div>
      </div>
      <div>
        <Label className="text-xs text-muted-foreground">{pick({ tr: "Notlar", en: "Notes" })}</Label>
        <Textarea className="mt-1.5" value={section.notes} onChange={(event) => onChange({ ...section, notes: event.target.value })} />
      </div>
      <NestedList title={pick({ tr: "Toplantılar", en: "Meetings" })} onAdd={() => onChange({ ...section, meetings: [...section.meetings, emptyMeeting()] })} addLabel={pick({ tr: "Toplantı ekle", en: "Add meeting" })}>
        {section.meetings.map((meeting, index) => (
          <div className="grid gap-2 rounded-lg border p-2 sm:grid-cols-2 lg:grid-cols-6" key={index}>
            <Field label={pick({ tr: "Gün", en: "Day" })} value={meeting.weekday} onChange={(weekday) => onChange({ ...section, meetings: updateAt(section.meetings, index, { weekday }) })} />
            <Field label={pick({ tr: "Başlangıç (dakika)", en: "Start (minute)" })} type="number" value={meeting.start_minute} onChange={(start_minute) => onChange({ ...section, meetings: updateAt(section.meetings, index, { start_minute }) })} />
            <Field label={pick({ tr: "Bitiş (dakika)", en: "End (minute)" })} type="number" value={meeting.end_minute} onChange={(end_minute) => onChange({ ...section, meetings: updateAt(section.meetings, index, { end_minute }) })} />
            <Field label={pick({ tr: "Salon", en: "Room" })} value={meeting.room} onChange={(room) => onChange({ ...section, meetings: updateAt(section.meetings, index, { room }) })} />
            <Field label={pick({ tr: "Durum", en: "Status" })} value={meeting.status} onChange={(status) => onChange({ ...section, meetings: updateAt(section.meetings, index, { status }) })} />
            <div className="flex items-end"><Button variant="ghost" size="sm" onClick={() => onChange({ ...section, meetings: section.meetings.filter((_, itemIndex) => itemIndex !== index) })}>{pick({ tr: "Kaldır", en: "Remove" })}</Button></div>
          </div>
        ))}
      </NestedList>
      <NestedList title={pick({ tr: "Eğitmenler", en: "Instructors" })} onAdd={() => onChange({ ...section, instructors: [...section.instructors, emptyInstructor()] })} addLabel={pick({ tr: "Eğitmen ekle", en: "Add instructor" })}>
        {section.instructors.map((instructor, index) => (
          <div className="grid gap-2 rounded-lg border p-2 sm:grid-cols-2 lg:grid-cols-5" key={index}>
            <Field label={pick({ tr: "Ad", en: "Name" })} value={instructor.name} onChange={(name) => onChange({ ...section, instructors: updateAt(section.instructors, index, { name }) })} />
            <Field label={pick({ tr: "Unvan", en: "Position" })} value={instructor.position} onChange={(position) => onChange({ ...section, instructors: updateAt(section.instructors, index, { position }) })} />
            <Field label="Researcher ID" value={instructor.researcher_id} onChange={(researcher_id) => onChange({ ...section, instructors: updateAt(section.instructors, index, { researcher_id }) })} />
            <Field label={pick({ tr: "Çözüm durumu", en: "Resolution" })} value={instructor.resolution_status} onChange={(resolution_status) => onChange({ ...section, instructors: updateAt(section.instructors, index, { resolution_status }) })} />
            <div className="flex items-end"><Button variant="ghost" size="sm" onClick={() => onChange({ ...section, instructors: section.instructors.filter((_, itemIndex) => itemIndex !== index) })}>{pick({ tr: "Kaldır", en: "Remove" })}</Button></div>
          </div>
        ))}
      </NestedList>
      <NestedList title={pick({ tr: "Kısıtlar", en: "Restrictions" })} onAdd={() => onChange({ ...section, restrictions: [...section.restrictions, emptyRestriction()] })} addLabel={pick({ tr: "Kısıt ekle", en: "Add restriction" })}>
        {section.restrictions.map((restriction, index) => (
          <div className="space-y-3 rounded-lg border p-3" key={index}>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
              <Field label={pick({ tr: "Tür", en: "Kind" })} value={restriction.kind} onChange={(kind) => updateRestriction(index, { kind })} />
              <Field label={pick({ tr: "Grup", en: "Group" })} value={restriction.restriction_group} onChange={(restriction_group) => updateRestriction(index, { restriction_group })} />
              <Field label={pick({ tr: "Satır", en: "Row" })} type="number" min="0" step="1" value={restriction.row_index} onChange={(row_index) => updateRestriction(index, { row_index })} />
              <Field label={pick({ tr: "Metin değeri", en: "Text value" })} value={restriction.value_text} onChange={(value_text) => updateRestriction(index, { value_text })} />
              <Field label={pick({ tr: "Sayısal değer", en: "Numeric value" })} type="number" step="0.01" value={restriction.value_numeric} onChange={(value_numeric) => updateRestriction(index, { value_numeric })} />
            </div>
            <details className="rounded-lg border bg-muted/20 p-3">
              <summary className="cursor-pointer text-sm font-medium">{pick({ tr: "Uygunluk ayrıntıları", en: "Eligibility details" })}</summary>
              <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
                <Field label={pick({ tr: "Operatör", en: "Operator" })} value={restriction.operator} onChange={(operator) => updateRestriction(index, { operator })} />
                <Field label={pick({ tr: "Minimum not", en: "Minimum grade" })} value={restriction.minimum_grade} onChange={(minimum_grade) => updateRestriction(index, { minimum_grade })} />
                <Field label={pick({ tr: "Kabul edilen bölüm", en: "Admitted department" })} value={restriction.given_department} onChange={(given_department) => updateRestriction(index, { given_department })} />
                <Field label={pick({ tr: "Soyadı başlangıcı", en: "Surname start" })} value={restriction.start_char} onChange={(start_char) => updateRestriction(index, { start_char })} />
                <Field label={pick({ tr: "Soyadı bitişi", en: "Surname end" })} value={restriction.end_char} onChange={(end_char) => updateRestriction(index, { end_char })} />
                <Field label={pick({ tr: "Minimum CGPA", en: "Minimum CGPA" })} type="number" step="0.01" min="0" value={restriction.min_cgpa} onChange={(min_cgpa) => updateRestriction(index, { min_cgpa })} />
                <Field label={pick({ tr: "Maksimum CGPA", en: "Maximum CGPA" })} type="number" step="0.01" min="0" value={restriction.max_cgpa} onChange={(max_cgpa) => updateRestriction(index, { max_cgpa })} />
                <Field label={pick({ tr: "Minimum yıl", en: "Minimum year" })} type="number" step="1" min="0" value={restriction.min_year} onChange={(min_year) => updateRestriction(index, { min_year })} />
                <Field label={pick({ tr: "Maksimum yıl", en: "Maximum year" })} type="number" step="1" min="0" value={restriction.max_year} onChange={(max_year) => updateRestriction(index, { max_year })} />
                <Field label={pick({ tr: "Önceki ders", en: "Prior course" })} value={restriction.prior_course_code} onChange={(prior_course_code) => updateRestriction(index, { prior_course_code })} />
                <Field label={pick({ tr: "Program kodu", en: "Program code" })} value={restriction.program_code} onChange={(program_code) => updateRestriction(index, { program_code })} />
                <Field label={pick({ tr: "Müfredat sürümü", en: "Curriculum version" })} value={restriction.curriculum_version} onChange={(curriculum_version) => updateRestriction(index, { curriculum_version })} />
                <Field label={pick({ tr: "Durum", en: "Status" })} value={restriction.status} onChange={(status) => updateRestriction(index, { status })} />
                <Field label={pick({ tr: "Başlangıç not aralığı", en: "Start grade band" })} value={restriction.start_grade} onChange={(start_grade) => updateRestriction(index, { start_grade })} maxLength={128} className="sm:col-span-2" />
                <Field label={pick({ tr: "Bitiş not aralığı", en: "End grade band" })} value={restriction.end_grade} onChange={(end_grade) => updateRestriction(index, { end_grade })} maxLength={128} className="sm:col-span-2" />
                <div className="space-y-1.5 sm:col-span-2 lg:col-span-4">
                  <Label className="text-xs text-muted-foreground">{pick({ tr: "Ham kaynak metni", en: "Raw source text" })}</Label>
                  <Textarea value={restriction.raw_text} onChange={(event) => updateRestriction(index, { raw_text: event.target.value })} maxLength={4000} />
                </div>
              </div>
            </details>
            <div className="flex flex-wrap items-center justify-end gap-2">
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={restriction.verified} onChange={(event) => updateRestriction(index, { verified: event.target.checked })} />
                {pick({ tr: "Doğrulandı", en: "Verified" })}
              </label>
              <Button variant="ghost" size="sm" onClick={() => onChange({ ...section, restrictions: section.restrictions.filter((_, itemIndex) => itemIndex !== index) })}>{pick({ tr: "Kaldır", en: "Remove" })}</Button>
            </div>
          </div>
        ))}
      </NestedList>
    </div>
  );
}

function NestedList({ title, onAdd, addLabel, children }: { title: string; onAdd: () => void; addLabel: string; children: ReactNode }) {
  return <div className="space-y-2"><div className="flex items-center justify-between gap-2"><h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h4><Button variant="ghost" size="xs" onClick={onAdd}>{addLabel}</Button></div>{children}</div>;
}

function RulesEditor({
  groups,
  replacements,
  onGroupsChange,
  onReplacementsChange,
}: {
  groups: FormPrerequisiteGroup[];
  replacements: FormReplacement[];
  onGroupsChange: (groups: FormPrerequisiteGroup[]) => void;
  onReplacementsChange: (replacements: FormReplacement[]) => void;
}) {
  const { pick } = useLocale();
  return <Card size="sm"><CardHeader><CardTitle className="text-sm">{pick({ tr: "Kurallar ve eşdeğerler", en: "Rules and replacements" })}</CardTitle><CardDescription>{pick({ tr: "Ön koşullar ve yerine geçen dersler ayrı ayrı saklanır.", en: "Prerequisites and replacement courses are stored separately." })}</CardDescription></CardHeader><CardContent className="space-y-4"><NestedList title={pick({ tr: "Ön koşul grupları", en: "Prerequisite groups" })} onAdd={() => onGroupsChange([...groups, emptyGroup()])} addLabel={pick({ tr: "Grup ekle", en: "Add group" })}>{groups.map((group, index) => <div className="space-y-3 rounded-lg border p-3" key={index}><div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5"><Field label={pick({ tr: "Grup", en: "Group" })} value={group.group_no} onChange={(group_no) => onGroupsChange(updateAt(groups, index, { group_no }))} /><Field label={pick({ tr: "Mantık", en: "Logic" })} value={group.logic} onChange={(logic) => onGroupsChange(updateAt(groups, index, { logic }))} /><Field label={pick({ tr: "Program", en: "Program" })} value={group.program_code} onChange={(program_code) => onGroupsChange(updateAt(groups, index, { program_code }))} /><Field label={pick({ tr: "Müfredat", en: "Curriculum" })} value={group.curriculum_version} onChange={(curriculum_version) => onGroupsChange(updateAt(groups, index, { curriculum_version }))} /><div className="flex items-end"><Button variant="destructive" size="sm" onClick={() => onGroupsChange(groups.filter((_, itemIndex) => itemIndex !== index))}>{pick({ tr: "Grubu kaldır", en: "Remove group" })}</Button></div></div><NestedList title={pick({ tr: "Gereksinimler", en: "Requirements" })} onAdd={() => onGroupsChange(updateAt(groups, index, { requirements: [...group.requirements, emptyRequirement()] }))} addLabel={pick({ tr: "Gereksinim ekle", en: "Add requirement" })}>{group.requirements.map((requirement, requirementIndex) => <div className="grid gap-2 rounded-lg border p-2 sm:grid-cols-2 lg:grid-cols-4" key={requirementIndex}><Field label={pick({ tr: "Ders kodu", en: "Course code" })} value={requirement.course_code} onChange={(course_code) => onGroupsChange(updateAt(groups, index, { requirements: updateAt(group.requirements, requirementIndex, { course_code }) }))} /><Field label={pick({ tr: "En düşük not", en: "Minimum grade" })} value={requirement.minimum_grade} onChange={(minimum_grade) => onGroupsChange(updateAt(groups, index, { requirements: updateAt(group.requirements, requirementIndex, { minimum_grade }) }))} /><Field label={pick({ tr: "Gereksinim türü", en: "Requirement type" })} value={requirement.requirement_type} onChange={(requirement_type) => onGroupsChange(updateAt(groups, index, { requirements: updateAt(group.requirements, requirementIndex, { requirement_type }) }))} /><div className="flex items-end"><Button variant="ghost" size="sm" onClick={() => onGroupsChange(updateAt(groups, index, { requirements: group.requirements.filter((_, itemIndex) => itemIndex !== requirementIndex) }))}>{pick({ tr: "Kaldır", en: "Remove" })}</Button></div></div>)}</NestedList></div>)}</NestedList><NestedList title={pick({ tr: "Yerine geçen dersler", en: "Replacement courses" })} onAdd={() => onReplacementsChange([...replacements, emptyReplacement()])} addLabel={pick({ tr: "Eşdeğer ekle", en: "Add replacement" })}>{replacements.map((replacement, index) => <div className="grid gap-2 rounded-lg border p-2 sm:grid-cols-2 lg:grid-cols-4" key={index}><Field label={pick({ tr: "Ders", en: "Course" })} value={replacement.course_code} onChange={(course_code) => onReplacementsChange(updateAt(replacements, index, { course_code }))} /><Field label={pick({ tr: "Yerine geçen", en: "Replacement" })} value={replacement.replacement_code} onChange={(replacement_code) => onReplacementsChange(updateAt(replacements, index, { replacement_code }))} /><Field label={pick({ tr: "İlişki", en: "Relation" })} value={replacement.relation} onChange={(relation) => onReplacementsChange(updateAt(replacements, index, { relation }))} /><div className="flex items-end"><Button variant="ghost" size="sm" onClick={() => onReplacementsChange(replacements.filter((_, itemIndex) => itemIndex !== index))}>{pick({ tr: "Kaldır", en: "Remove" })}</Button></div></div>)}</NestedList></CardContent></Card>;
}

function CreateDraftDialog({
  term,
  pending,
  onClose,
  onSubmit,
}: {
  term: string;
  pending: boolean;
  onClose: () => void;
  onSubmit: (payload: { course_code: string; data?: CatalogDraftPatch; reason: string }) => void;
}) {
  const { pick } = useLocale();
  const [courseCode, setCourseCode] = useState("");
  const [title, setTitle] = useState("");
  const [credits, setCredits] = useState("");
  const [ects, setEcts] = useState("");
  const [reason, setReason] = useState("");
  const normalizedCourseCode = courseCode.trim().replace(/[\s-]/g, "");
  const valid = /^\d{7}$/.test(normalizedCourseCode) && reason.trim().length >= 3;
  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}><DialogContent><DialogHeader><DialogTitle>{pick({ tr: "Ders taslağı oluştur", en: "Create course draft" })}</DialogTitle><DialogDescription>{pick({ tr: `${term} dönemi için typed bir başlangıç taslağı oluşturun.`, en: `Create a typed starter draft for term ${term}.` })}</DialogDescription></DialogHeader><div className="space-y-3"><Field label={pick({ tr: "Ders kodu (7 haneli ODTÜ kodu)", en: "Course code (7-digit METU code)" })} value={courseCode} onChange={setCourseCode} placeholder="2402201" /><Field label={pick({ tr: "Başlık (isteğe bağlı)", en: "Title (optional)" })} value={title} onChange={setTitle} /><div className="grid gap-3 sm:grid-cols-2"><Field label={pick({ tr: "Kredi", en: "Credits" })} type="number" step="0.5" value={credits} onChange={setCredits} /><Field label="ECTS" type="number" step="0.5" value={ects} onChange={setEcts} /></div><div className="space-y-1.5"><Label htmlFor="create-draft-reason">{pick({ tr: "Neden", en: "Reason" })}</Label><Textarea id="create-draft-reason" value={reason} onChange={(event) => setReason(event.target.value)} /></div></div><DialogFooter><Button variant="outline" onClick={onClose} disabled={pending}>{pick({ tr: "Vazgeç", en: "Cancel" })}</Button><Button disabled={!valid || pending} onClick={() => onSubmit({ course_code: normalizedCourseCode, data: { title: title.trim() || undefined, local_credits: asNumber(credits), ects: asNumber(ects) }, reason: reason.trim() })}>{pending ? <Loader2Icon className="animate-spin" /> : <BookOpenIcon />}{pick({ tr: "Taslak oluştur", en: "Create draft" })}</Button></DialogFooter></DialogContent></Dialog>;
}

function ImportDialog({
  selectedCodes,
  term,
  pending,
  onClose,
  onSubmit,
}: {
  selectedCodes: string[];
  term: string;
  pending: boolean;
  onClose: () => void;
  onSubmit: (payload: { department: string; course_codes: string[]; reason: string }) => void;
}) {
  const { pick } = useLocale();
  const [department, setDepartment] = useState("");
  const [scope, setScope] = useState(selectedCodes.length ? "selected" : "department");
  const [reason, setReason] = useState("");
  const valid = reason.trim().length >= 3 && (scope === "selected" ? selectedCodes.length > 0 : department.trim().length > 0);
  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}><DialogContent><DialogHeader><DialogTitle>{pick({ tr: "Katalog yenilemesi iste", en: "Request catalog refresh" })}</DialogTitle><DialogDescription>{pick({ tr: `${term} dönemi için kaynaklardan yeniden alım bir arka plan işi olarak çalışır.`, en: `The source refresh for ${term} runs as a background job.` })}</DialogDescription></DialogHeader><div className="space-y-3"><div className="space-y-1.5"><Label htmlFor="catalog-import-scope">{pick({ tr: "Kapsam", en: "Scope" })}</Label><select id="catalog-import-scope" value={scope} onChange={(event) => setScope(event.target.value)} className="h-9 w-full rounded-lg border bg-background px-3 text-sm"><option value="selected" disabled={!selectedCodes.length}>{selectedCodes.length ? pick({ tr: `Seçilen dersler (${selectedCodes.length})`, en: `Selected courses (${selectedCodes.length})` }) : pick({ tr: "Seçilen ders yok", en: "No courses selected" })}</option><option value="department">{pick({ tr: "Bölüm", en: "Department" })}</option></select></div>{scope === "department" ? <Field label={pick({ tr: "Bölüm kodu", en: "Department code" })} value={department} onChange={setDepartment} placeholder="CNG" /> : <p className="rounded-lg border bg-muted/30 p-3 text-sm text-muted-foreground">{selectedCodes.join(", ")}</p>}<div className="space-y-1.5"><Label htmlFor="catalog-import-reason">{pick({ tr: "Neden", en: "Reason" })}</Label><Textarea id="catalog-import-reason" value={reason} onChange={(event) => setReason(event.target.value)} /></div></div><DialogFooter><Button variant="outline" onClick={onClose} disabled={pending}>{pick({ tr: "Vazgeç", en: "Cancel" })}</Button><Button disabled={!valid || pending} onClick={() => onSubmit({ department: scope === "department" ? department : "", course_codes: scope === "selected" ? selectedCodes : [], reason: reason.trim() })}>{pending ? <Loader2Icon className="animate-spin" /> : <RefreshCwIcon />}{pick({ tr: "Yenilemeyi sıraya al", en: "Queue refresh" })}</Button></DialogFooter></DialogContent></Dialog>;
}

function PublishDialog({
  selectedRows,
  conflictTotal,
  pending,
  onClose,
  onSubmit,
}: {
  selectedRows: CatalogCourseRow[];
  conflictTotal: number;
  pending: boolean;
  onClose: () => void;
  onSubmit: (payload: { reason: string; acknowledge_conflicts: boolean }) => void;
}) {
  const { pick } = useLocale();
  const [acknowledge, setAcknowledge] = useState(false);
  const [reason, setReason] = useState("");
  const draftRows = selectedRows.filter((row) => Boolean(row.draft_id));
  const valid = draftRows.length > 0 && reason.trim().length >= 3 && (!conflictTotal || acknowledge);
  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}><DialogContent><DialogHeader><DialogTitle>{pick({ tr: "Yayınlamayı incele", en: "Review publish" })}</DialogTitle><DialogDescription>{pick({ tr: "Yalnızca seçtiğiniz taslaklar yayınlanır. Yayın sürümünü değiştirecek işlemi onaylayın.", en: "Only the selected drafts will be published. Review the release change before confirming." })}</DialogDescription></DialogHeader><div className="space-y-3"><div className="rounded-lg border p-3 text-sm"><p className="font-medium">{pick({ tr: `${draftRows.length} taslak`, en: `${draftRows.length} drafts` })}</p><p className="mt-1 text-muted-foreground">{draftRows.map((row) => row.course_code).join(", ") || pick({ tr: "Seçilenlerde taslak yok.", en: "The selection has no drafts." })}</p></div>{conflictTotal ? <label className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/[0.06] p-3 text-sm"><input type="checkbox" checked={acknowledge} onChange={(event) => setAcknowledge(event.target.checked)} className="mt-0.5" /><span><strong>{conflictTotal} {pick({ tr: "kaynak çakışmasını", en: "source conflicts" })}</strong> {pick({ tr: "incelediğimi ve mevcut override'ları koruduğumu onaylıyorum.", en: "reviewed and want to retain the current overrides." })}</span></label> : null}<div className="space-y-1.5"><Label htmlFor="catalog-publish-reason">{pick({ tr: "Yayın nedeni", en: "Publish reason" })}</Label><Textarea id="catalog-publish-reason" value={reason} onChange={(event) => setReason(event.target.value)} /></div></div><DialogFooter><Button variant="outline" onClick={onClose} disabled={pending}>{pick({ tr: "Vazgeç", en: "Cancel" })}</Button><Button disabled={!valid || pending} onClick={() => onSubmit({ reason: reason.trim(), acknowledge_conflicts: acknowledge })}>{pending ? <Loader2Icon className="animate-spin" /> : <SendIcon />}{pick({ tr: "Yayınla", en: "Publish" })}</Button></DialogFooter></DialogContent></Dialog>;
}

function RollbackDialog({
  release,
  pending,
  onClose,
  onSubmit,
}: {
  release: CatalogRelease;
  pending: boolean;
  onClose: () => void;
  onSubmit: (payload: { target_release_id: string; reason: string }) => void;
}) {
  const { pick, locale } = useLocale();
  const [reason, setReason] = useState("");
  const valid = reason.trim().length >= 3;
  return <Dialog open onOpenChange={(open) => { if (!open) onClose(); }}><DialogContent><DialogHeader><DialogTitle>{pick({ tr: "Kataloğu geri al", en: "Rollback catalog" })}</DialogTitle><DialogDescription>{pick({ tr: "Bu işlem seçilen sürümü yeniden etkinleştirir ve denetim kaydı oluşturur.", en: "This reactivates the selected release and creates an audit record." })}</DialogDescription></DialogHeader><div className="space-y-3"><div className="rounded-lg border border-amber-500/40 bg-amber-500/[0.06] p-3 text-sm"><p className="font-medium">{release.id}</p><p className="mt-1 text-muted-foreground">{formatDate(release.created_at, locale)} · {release.course_count ?? "—"} {pick({ tr: "ders", en: "courses" })}</p></div><div className="space-y-1.5"><Label htmlFor="catalog-rollback-reason">{pick({ tr: "Geri alma nedeni", en: "Rollback reason" })}</Label><Textarea id="catalog-rollback-reason" value={reason} onChange={(event) => setReason(event.target.value)} /></div></div><DialogFooter><Button variant="outline" onClick={onClose} disabled={pending}>{pick({ tr: "Vazgeç", en: "Cancel" })}</Button><Button variant="destructive" disabled={!valid || pending} onClick={() => onSubmit({ target_release_id: release.id, reason: reason.trim() })}>{pending ? <Loader2Icon className="animate-spin" /> : <RotateCcwIcon />}{pick({ tr: "Geri al", en: "Rollback" })}</Button></DialogFooter></DialogContent></Dialog>;
}

function ImportJobsPanel({
  data,
  loading,
  error,
  canWrite,
  onRefresh,
  onRequest,
}: {
  data?: CatalogImportsResponse;
  loading: boolean;
  error: Error | null;
  canWrite: boolean;
  onRefresh: () => void;
  onRequest: () => void;
}) {
  const { pick, locale } = useLocale();
  const jobs = importItems(data);
  if (loading) return <Skeleton className="h-64 rounded-xl" />;
  if (error) return <ErrorState error={error} retry={onRefresh} />;
  return <Card><CardHeader className="flex flex-wrap items-center justify-between gap-3"><div><CardTitle>{pick({ tr: "Katalog yenileme işleri", en: "Catalog refresh jobs" })}</CardTitle><CardDescription>{pick({ tr: "Kaynak alımlarının ilerlemesini ve çakışma sonuçlarını izleyin.", en: "Track source refresh progress and conflict results." })}</CardDescription></div><div className="flex gap-2"><Button variant="outline" size="sm" onClick={onRefresh}><RefreshCwIcon />{pick({ tr: "Yenile", en: "Refresh" })}</Button>{canWrite ? <Button size="sm" onClick={onRequest}><UploadCloudIcon />{pick({ tr: "İş başlat", en: "Start job" })}</Button> : null}</div></CardHeader><CardContent className="space-y-3">{jobs.length ? jobs.map((job) => { const progressState = importProgress(job); const progress = progressState.total > 0 ? Math.round((progressState.processed / progressState.total) * 100) : job.status === "completed" ? 100 : 0; const scope = job.course_codes?.join(", ") || job.department || pick({ tr: "Genel katalog", en: "Whole catalog" }); return <div className="space-y-2 rounded-xl border p-3" key={job.id}><div className="flex flex-wrap items-center gap-2"><span className="font-mono text-sm">{job.id}</span><StatusBadge value={job.status} /><span className="text-xs text-muted-foreground">{progressState.phase}</span><span className="text-xs text-muted-foreground">{scope}</span><span className="ml-auto text-xs text-muted-foreground">{formatDate(job.updated_at, locale)}</span></div><Progress value={progress} /><div className="flex flex-wrap justify-between gap-2 text-xs text-muted-foreground"><span>{progressState.processed}/{progressState.total || "—"}</span>{progressState.conflicts ? <span className="text-amber-700 dark:text-amber-300">{progressState.conflicts} {pick({ tr: "çakışma", en: "conflicts" })}</span> : null}{progressState.error ? <span className="text-destructive">{progressState.error}</span> : null}</div></div>; }) : <EmptyState title={pick({ tr: "Yenileme işi yok", en: "No refresh jobs" })} description={pick({ tr: "Bir ders veya bölüm seçip yenileme isteyebilirsiniz.", en: "Request a refresh for selected courses or a department." })} icon={<Clock3Icon className="size-4" />} />}</CardContent></Card>;
}

function ReleaseHistoryPanel({
  data,
  loading,
  error,
  canWrite,
  onRefresh,
  onRollback,
}: {
  data?: CatalogReleasesResponse;
  loading: boolean;
  error: Error | null;
  canWrite: boolean;
  onRefresh: () => void;
  onRollback: (release: CatalogRelease) => void;
}) {
  const { pick, locale } = useLocale();
  const releases = releaseItems(data);
  const activeId = data?.active_release_id;
  if (loading) return <Skeleton className="h-64 rounded-xl" />;
  if (error) return <ErrorState error={error} retry={onRefresh} />;
  return <Card><CardHeader><CardTitle>{pick({ tr: "Yayın geçmişi", en: "Release history" })}</CardTitle><CardDescription>{pick({ tr: "Aktif sürümü ve geri alınabilir yayınları inceleyin.", en: "Review the active release and releases available for rollback." })}</CardDescription></CardHeader><CardContent>{releases.length ? <div className="space-y-2">{releases.map((release) => <div className="flex flex-wrap items-center gap-3 rounded-xl border p-3" key={release.id}><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><span className="font-mono text-sm">{release.id}</span>{activeId === release.id || release.active || release.status === "active" ? <Badge variant="secondary">{pick({ tr: "Aktif", en: "Active" })}</Badge> : <StatusBadge value={releaseStatus(release)} />}</div><p className="mt-1 text-xs text-muted-foreground">r{release.release_number ?? release.revision ?? "—"} · {release.course_count ?? "—"} {pick({ tr: "ders", en: "courses" })} · {formatDate(release.published_at ?? release.created_at, locale)}</p>{release.reason ? <p className="mt-1 text-sm">{release.reason}</p> : null}</div>{canWrite && activeId !== release.id && !release.active && release.status !== "active" ? <Button variant="outline" size="sm" onClick={() => onRollback(release)}><RotateCcwIcon />{pick({ tr: "Geri al", en: "Rollback" })}</Button> : null}</div>)}</div> : <EmptyState title={pick({ tr: "Yayın yok", en: "No releases" })} icon={<HistoryIcon className="size-4" />} />}</CardContent></Card>;
}
