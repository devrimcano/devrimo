/**
 * Administrator catalog domain types used by the UI.
 *
 * The route response models are generated into `lib/api/schema.ts` from the
 * backend OpenAPI document. These hand-written types remain the UI-facing
 * aliases and compatibility layer for richer nested values and older
 * payloads; changes to the route contract must be reflected in both files
 * and verified with `npm run contracts:check`.
 */

import type { components as ApiComponents } from "@/lib/api/schema";

export type CatalogState =
  | "published"
  | "draft"
  | "unavailable"
  | "unknown"
  | "blocked"
  | string;

/** Component coverage returned by the catalog service. */
export type CatalogCompleteness = {
  listing: boolean;
  details: boolean;
  sections: boolean;
  constraints: boolean;
  prerequisites: boolean;
  replacements: boolean;
};

export type CatalogFreshness = "fresh" | "stale" | "unknown" | string;

export type CatalogMeetingStatus = "verified" | "untimed" | "unknown" | "unpublished" | "invalid";

export type CatalogIssueSeverity = "error" | "warning" | "info" | string;

export type CatalogIssue = {
  code?: string | null;
  field?: string | null;
  severity?: CatalogIssueSeverity | null;
  message: string;
  source?: string | null;
  [key: string]: unknown;
};

export type CatalogSourceConflict = {
  field: string;
  source_value: unknown;
  override_value: unknown;
  source_observed_at?: string | null;
  source_status?: string | null;
  override_reason?: string | null;
  [key: string]: unknown;
};

export type CatalogCourseRow = {
  course_code: string;
  /** "CENG 331": the spelling students, timetables and doors use. */
  display_code?: string | null;
  department: string | null;
  title: string | null;
  local_credits: number | null;
  ects: number | null;
  level: string | null;
  availability: string | null;
  state: CatalogState;
  completeness: CatalogCompleteness;
  freshness: CatalogFreshness;
  source_conflicts: boolean | number | CatalogSourceConflict[];
  draft_id: string | null;
  course_revision_id: string | null;
  section_count: number;
  _catalog: CatalogReleaseMetadata;
  [key: string]: unknown;
};

export type CatalogCoverageCounts = {
  listed?: number;
  detailed?: number;
  schedulable?: number;
  restrictions_verified?: number;
  prerequisites_verified?: number;
  restriction_verified?: number;
  prerequisite_verified?: number;
  fresh?: number;
  stale?: number;
  source_conflicts?: number;
  conflicts?: number;
  [key: string]: number | undefined;
};

export type CatalogCourseListResponse = {
  courses: CatalogCourseRow[];
  total: number;
  release_id: string | null;
  counts: CatalogCoverageCounts;
  term?: string | null;
};

export type CatalogMeeting = {
  weekday: number | null;
  start_minute: number | null;
  end_minute: number | null;
  room: string | null;
  status: string;
  raw_label?: string | null;
  [key: string]: unknown;
};

export type CatalogInstructor = {
  source_name: string;
  name?: string;
  position: string | null;
  researcher_id: number | null;
  match_method?: string;
  resolution_status: string;
  [key: string]: unknown;
};

export type CatalogRestriction = {
  kind: string;
  value_text: string | null;
  value_numeric: number | null;
  minimum_grade: string | null;
  program_code: string | null;
  curriculum_version: string | null;
  verified: boolean;
  status?: string;
  restriction_group?: string | null;
  row_index?: number;
  operator?: string | null;
  given_department?: string | null;
  start_char?: string | null;
  end_char?: string | null;
  min_cgpa?: number | null;
  max_cgpa?: number | null;
  min_year?: number | null;
  max_year?: number | null;
  start_grade?: string | null;
  end_grade?: string | null;
  prior_course_code?: string | null;
  raw_text?: string | null;
  [key: string]: unknown;
};

export type CatalogSection = {
  id?: string;
  section_code: string;
  status: string;
  notes: string | null;
  syllabus_url?: string | null;
  syllabus_available?: boolean | null;
  meetings_status: CatalogMeetingStatus;
  meetings: CatalogMeeting[];
  instructors: CatalogInstructor[];
  restrictions: CatalogRestriction[];
  [key: string]: unknown;
};

export type CatalogRequirement = {
  course_code: string;
  minimum_grade: string | null;
  requirement_type: string;
  position?: number;
  raw_text?: string | null;
  [key: string]: unknown;
};

export type CatalogPrerequisiteGroup = {
  group_no: number | string;
  logic: string;
  program_code: string | null;
  curriculum_version: string | null;
  requirements: CatalogRequirement[];
  applicability?: Record<string, unknown>;
  verified?: boolean;
  raw_text?: string | null;
  [key: string]: unknown;
};

export type CatalogReplacement = {
  course_code?: string | null;
  replacement_code?: string | null;
  equivalent_code?: string | null;
  relationship_type?: string | null;
  related_course_code?: string | null;
  relation?: string | null;
  [key: string]: unknown;
};

export type CatalogSourceObservation = {
  id?: string;
  tool?: string | null;
  arguments?: unknown;
  payload?: unknown;
  candidate_data?: unknown;
  status?: string | null;
  observed_at?: string | null;
  source_fetched_at?: string | null;
  parser_version?: string | null;
  issues?: unknown[] | null;
  // Legacy summary keys remain optional while older observations are read.
  component?: string | null;
  source?: string | null;
  source_status?: string | null;
  fetched_at?: string | null;
  fresh?: boolean | null;
  verified?: boolean | null;
  issue?: string | null;
  term?: string | null;
  department?: string | null;
  course_code?: string | null;
  section_code?: string | null;
  [key: string]: unknown;
};

export type CatalogSourceObservationDetail = CatalogSourceObservation & {
  id: string;
  tool: string;
  arguments: unknown;
  payload: unknown;
  candidate_data: unknown;
  status: string;
  observed_at: string | null;
  source_fetched_at: string | null;
  parser_version: string;
  issues: unknown[];
};

export type CatalogHistoryEntry = {
  id?: string;
  action: string;
  actor?: string | null;
  reason?: string | null;
  created_at: string;
  revision?: number | null;
  changes?: Record<string, unknown> | null;
  [key: string]: unknown;
};

export type CatalogReleaseMetadata = {
  release_id: string | null;
  course_revision_id?: string | null;
  course_revision_ids?: Record<string, string>;
  components?: Record<string, {
    fresh?: boolean | null;
    verified?: boolean | null;
    observed_at?: string | null;
    source_status?: string | null;
    [key: string]: unknown;
  }>;
  [key: string]: unknown;
};

export type CatalogCourseDetail = {
  course_code: string;
  term: string | null;
  department: string | null;
  title: string | null;
  credits?: number | null;
  local_credits?: number | null;
  ects: number | null;
  level: string | null;
  availability: string | null;
  campus?: string | null;
  is_thesis?: boolean;
  component_status?: Record<string, unknown>;
  state: CatalogState;
  completeness: CatalogCompleteness;
  sections: CatalogSection[];
  prerequisite_groups: CatalogPrerequisiteGroup[];
  replacements: CatalogReplacement[];
  source_observations: CatalogSourceObservation[];
  history: CatalogHistoryEntry[];
  _catalog: CatalogReleaseMetadata;
  source_conflicts?: CatalogSourceConflict[];
  issues?: CatalogIssue[];
  field_overrides?: Record<string, unknown>;
  draft_id?: string | null;
  draft_revision?: number | null;
  draft?: CatalogDraft | null;
  [key: string]: unknown;
};

export type CatalogDraft = {
  id: string;
  revision: number;
  state: CatalogState;
  term?: string | null;
  course_code?: string | null;
  term_id: string;
  course_id: string;
  base_revision_id?: string | null;
  published_revision_id?: string | null;
  data: Partial<CatalogCourseDetail> & Record<string, unknown>;
  issues: CatalogIssue[];
  field_overrides?: Record<string, unknown>;
  reason?: string | null;
  created_by?: string | null;
  updated_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  [key: string]: unknown;
};

export type CatalogDraftPatch = {
  title?: string;
  department?: string | null;
  local_credits?: number | null;
  ects?: number | null;
  level?: string | null;
  availability?: string | null;
  campus?: string | null;
  is_thesis?: boolean;
  sections?: Array<{
    id?: string;
    section_code: string;
    status: string;
    notes: string | null;
    syllabus_url?: string | null;
    syllabus_available?: boolean | null;
    meetings_status: CatalogMeetingStatus;
    meetings: Array<{
      weekday: number | null;
      start_minute: number | null;
      end_minute: number | null;
      room?: string | null;
      status: string;
      raw_label?: string | null;
      [key: string]: unknown;
    }>;
    instructors: Array<{
      source_name: string;
      position?: string | null;
      researcher_id?: number | null;
      match_method?: string;
      resolution_status?: string;
      [key: string]: unknown;
    }>;
    restrictions: Array<{
      restriction_group?: string | null;
      row_index?: number;
      kind: string;
      value_text?: string | null;
      value_numeric?: number | null;
      operator?: string | null;
      minimum_grade?: string | null;
      given_department?: string | null;
      start_char?: string | null;
      end_char?: string | null;
      min_cgpa?: number | null;
      max_cgpa?: number | null;
      min_year?: number | null;
      max_year?: number | null;
      start_grade?: string | null;
      end_grade?: string | null;
      prior_course_code?: string | null;
      program_code?: string | null;
      curriculum_version?: string | null;
      raw_text?: string | null;
      verified: boolean;
      status?: string;
      [key: string]: unknown;
    }>;
  }>;
  prerequisite_groups?: CatalogPrerequisiteGroup[];
  replacements?: Array<{
    relationship_type: string;
    related_course_code: string;
    program_code?: string | null;
    curriculum_version?: string | null;
    verified?: boolean;
    [key: string]: unknown;
  }>;
  [key: string]: unknown;
};

export type CatalogCreateDraftRequest = {
  term: string;
  course_code: string;
  base_revision_id?: string | null;
  data?: CatalogDraftPatch;
  reason: string;
};

export type CatalogUpdateDraftRequest = {
  expected_revision: number;
  patch: CatalogDraftPatch;
  reason: string;
  verify?: boolean;
  verification_evidence?: string | null;
};

export type CatalogPublishRequest = {
  term: string;
  draft_ids: string[];
  expected_release_id: string | null;
  acknowledge_conflicts: boolean;
  idempotency_key: string;
  reason: string;
};

export type CatalogRollbackRequest = {
  term: string;
  target_release_id: string;
  expected_release_id: string | null;
  idempotency_key: string;
  reason: string;
};

export type CatalogImportRequest = {
  term: string;
  department?: string | null;
  course_codes?: string[];
  reason: string;
};

export type CatalogImportJob = {
  id: string;
  organization_id: string;
  term: string;
  department: string | null;
  course_codes: string[];
  status: string;
  dedup_key: string;
  payload?: Record<string, unknown>;
  checkpoint?: Record<string, unknown>;
  attempts?: number;
  lease_until?: string | null;
  error_code?: string | null;
  error_detail?: string | null;
  created_at: string | null;
  updated_at: string | null;
  started_at?: string | null;
  completed_at: string | null;
  [key: string]: unknown;
};

export type CatalogImportsResponse = {
  imports: CatalogImportJob[];
  total?: number;
  [key: string]: unknown;
};

export type CatalogRelease = {
  id: string;
  term?: string | null;
  release_number?: number | null;
  operation?: string | null;
  active?: boolean;
  revision?: number | null;
  status?: string | null;
  course_count?: number | null;
  source_conflicts?: number | null;
  metadata?: Record<string, unknown>;
  created_at: string | null;
  published_at?: string | null;
  rolled_back_from_release_id?: string | null;
  reason?: string | null;
  [key: string]: unknown;
};

export type CatalogReleasesResponse = {
  releases: CatalogRelease[];
  active_release_id?: string | null;
  term?: string | null;
  [key: string]: unknown;
};

export type CatalogOperationResponse = {
  operation_id: string;
  operation: string;
  status: string;
  term: string;
  release_id: string | null;
  target_release_id: string | null;
  idempotency_key: string;
  release_number?: number | null;
  published_draft_ids?: string[];
  course_revision_ids?: string[];
  course_count?: number | null;
  [key: string]: unknown;
};

export function issueMessage(issue: CatalogIssue | string): string {
  return typeof issue === "string" ? issue : issue.message;
}

export function conflictCount(value: number | boolean | CatalogSourceConflict[] | null | undefined): number {
  return Array.isArray(value) ? value.length : typeof value === "boolean" ? (value ? 1 : 0) : Number(value ?? 0);
}

// Keep the UI aliases checked against the generated response envelopes.  The
// aliases intentionally make fields required where the UI needs them and add
// richer nested shapes, so they should remain assignable to the backend
// contract even as the OpenAPI generator evolves.
type _AssertOpenApiCompatibility<T extends true> = T;
type _CatalogCourseRowOpenApiCompatibility = _AssertOpenApiCompatibility<
  CatalogCourseRow extends ApiComponents["schemas"]["CatalogCourseRowOut"] ? true : false
>;
type _CatalogCourseListOpenApiCompatibility = _AssertOpenApiCompatibility<
  Pick<CatalogCourseListResponse, "courses" | "total" | "release_id" | "term"> extends
    Pick<ApiComponents["schemas"]["CatalogCourseListOut"], "courses" | "total" | "release_id" | "term">
    ? true
    : false
>;
type _CatalogCourseDetailOpenApiCompatibility = _AssertOpenApiCompatibility<
  CatalogCourseDetail extends ApiComponents["schemas"]["CatalogCourseDetailOut"] ? true : false
>;
type _CatalogDraftOpenApiCompatibility = _AssertOpenApiCompatibility<
  CatalogDraft extends ApiComponents["schemas"]["CatalogDraftOut"] ? true : false
>;
type _CatalogImportJobOpenApiCompatibility = _AssertOpenApiCompatibility<
  CatalogImportJob extends ApiComponents["schemas"]["CatalogImportJobOut"] ? true : false
>;
type _CatalogImportsOpenApiCompatibility = _AssertOpenApiCompatibility<
  CatalogImportsResponse extends ApiComponents["schemas"]["CatalogImportsOut"] ? true : false
>;
type _CatalogReleaseOpenApiCompatibility = _AssertOpenApiCompatibility<
  CatalogRelease extends ApiComponents["schemas"]["CatalogReleaseOut"] ? true : false
>;
type _CatalogReleasesOpenApiCompatibility = _AssertOpenApiCompatibility<
  CatalogReleasesResponse extends ApiComponents["schemas"]["CatalogReleasesOut"] ? true : false
>;
type _CatalogOperationOpenApiCompatibility = _AssertOpenApiCompatibility<
  CatalogOperationResponse extends ApiComponents["schemas"]["CatalogOperationOut"] ? true : false
>;

// Generated response properties are optional when FastAPI fields have
// defaults, so the UI normalizes presence at its fetch boundary.  Required
// picks below preserve that rule while checking the consumed scalar values,
// including nullability.  Deliberately open nested source payloads stay out
// of this assertion.
type _GeneratedScalarsToUi<Ui, Api, Keys extends keyof Ui & keyof Api> =
  Required<Pick<Api, Keys>> extends Pick<Ui, Keys> ? true : false;

type _CatalogCourseRowGeneratedNullability = _AssertOpenApiCompatibility<
  _GeneratedScalarsToUi<CatalogCourseRow, ApiComponents["schemas"]["CatalogCourseRowOut"],
    "department" | "title" | "local_credits" | "ects" | "level" | "availability" | "campus" | "draft_id" | "course_revision_id">
>;
type _CatalogCourseDetailGeneratedNullability = _AssertOpenApiCompatibility<
  _GeneratedScalarsToUi<CatalogCourseDetail, ApiComponents["schemas"]["CatalogCourseDetailOut"],
    "term" | "department" | "title" | "credits" | "local_credits" | "ects" | "level" | "availability" | "campus" | "draft_id" | "draft_revision">
>;
type _CatalogDraftGeneratedNullability = _AssertOpenApiCompatibility<
  _GeneratedScalarsToUi<CatalogDraft, ApiComponents["schemas"]["CatalogDraftOut"],
    "term" | "course_code" | "base_revision_id" | "published_revision_id" | "reason" | "created_by" | "updated_by" | "created_at" | "updated_at">
>;
type _CatalogImportJobGeneratedNullability = _AssertOpenApiCompatibility<
  _GeneratedScalarsToUi<CatalogImportJob, ApiComponents["schemas"]["CatalogImportJobOut"],
    "department" | "lease_until" | "error_code" | "error_detail" | "created_at" | "updated_at" | "started_at" | "completed_at">
>;
type _CatalogReleaseGeneratedNullability = _AssertOpenApiCompatibility<
  _GeneratedScalarsToUi<CatalogRelease, ApiComponents["schemas"]["CatalogReleaseOut"],
    "release_number" | "operation" | "reason" | "created_at" | "expected_release_id" | "target_release_id">
>;
type _CatalogOperationGeneratedNullability = _AssertOpenApiCompatibility<
  _GeneratedScalarsToUi<CatalogOperationResponse, ApiComponents["schemas"]["CatalogOperationOut"],
    "release_id" | "target_release_id" | "release_number" | "course_count">
>;

/** Compile-time guard consumed by contract checks and IDE type checking. */
export type CatalogOpenApiCompatibility = [
  _CatalogCourseRowOpenApiCompatibility,
  _CatalogCourseListOpenApiCompatibility,
  _CatalogCourseDetailOpenApiCompatibility,
  _CatalogDraftOpenApiCompatibility,
  _CatalogImportJobOpenApiCompatibility,
  _CatalogImportsOpenApiCompatibility,
  _CatalogReleaseOpenApiCompatibility,
  _CatalogReleasesOpenApiCompatibility,
  _CatalogOperationOpenApiCompatibility,
  _CatalogCourseRowGeneratedNullability,
  _CatalogCourseDetailGeneratedNullability,
  _CatalogDraftGeneratedNullability,
  _CatalogImportJobGeneratedNullability,
  _CatalogReleaseGeneratedNullability,
  _CatalogOperationGeneratedNullability,
];
