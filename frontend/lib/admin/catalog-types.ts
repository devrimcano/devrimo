/**
 * The administrator catalog contract.
 *
 * This intentionally lives outside the generated OpenAPI file. The catalog
 * router is being rolled out alongside the UI and its payload has a richer
 * review model than the legacy planner endpoints. Keeping the types here
 * makes the admin surface explicit without teaching generated clients about
 * an endpoint that may still be behind a rollout flag.
 */

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
  department: string | null;
  title: string;
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
  term: string;
  department: string | null;
  title: string;
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
  term: string;
  course_code: string;
  data: Partial<CatalogCourseDetail> & Record<string, unknown>;
  issues: CatalogIssue[];
  field_overrides?: Record<string, unknown>;
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
  term: string;
  department: string | null;
  course_codes: string[] | null;
  status: string;
  payload?: Record<string, unknown>;
  checkpoint?: Record<string, unknown>;
  attempts?: number;
  lease_until?: string | null;
  error_code?: string | null;
  error_detail?: string | null;
  created_at: string;
  updated_at: string;
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
  created_at: string;
  published_at?: string | null;
  rolled_back_from_release_id?: string | null;
  reason?: string | null;
  [key: string]: unknown;
};

export type CatalogReleasesResponse = {
  releases: CatalogRelease[];
  active_release_id?: string | null;
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
  course_count?: number;
  [key: string]: unknown;
};

export function issueMessage(issue: CatalogIssue | string): string {
  return typeof issue === "string" ? issue : issue.message;
}

export function conflictCount(value: number | boolean | CatalogSourceConflict[] | null | undefined): number {
  return Array.isArray(value) ? value.length : typeof value === "boolean" ? (value ? 1 : 0) : Number(value ?? 0);
}
