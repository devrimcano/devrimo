"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api/errors";
import { jsonFetch } from "@/lib/api/fetcher";

export type PlanDay = "Mon" | "Tue" | "Wed" | "Thu" | "Fri";

export type PlanMeeting = {
  day: PlanDay;
  start_minute: number;
  duration_minutes: number;
  room: string;
};

export type PlanEntry = {
  id: string;
  code: string;
  name: string;
  section: string;
  credits: number;
  color: number;
  kind: "course" | "block";
  instructor: string;
  day: PlanDay;
  start_minute: number;
  duration_minutes: number;
  room: string;
};

export type PlanCourse = {
  code: string;
  name: string;
  credits: number;
  raw_code?: string | null;
  rawCode?: string;
  required?: boolean;
  [key: string]: unknown;
};

export type PlanState = {
  state_version?: number;
  entries: PlanEntry[];
  department: string;
  department_label: string;
  empty_days: PlanDay[];
  avoid_conflicts: boolean;
  ignore_constraints: boolean;
  pool: PlanCourse[];
  sections: Record<string, unknown[]>;
  alternatives: PlanEntry[][];
  alternative_index: number;
  favorites: PlanEntry[][];
  favorite_index: number;
  what_if?: boolean;
  what_if_backup?: Record<string, unknown> | null;
  locked_sections?: Record<string, string>;
  unscheduled_courses?: string[];
  generation_error?: string;
};

export type PlanEnvelope = {
  resource: "timetable";
  user_id: string;
  term: string;
  revision: number;
  updated_at: string | null;
  state: PlanState;
  can_undo: boolean;
  previous_revision: number | null;
  operation?: string | null;
  idempotency_key?: string | null;
  courses?: unknown[];
  busy_blocks?: unknown[];
};

export type PlanChanges = {
  operation:
    | "replace"
    | "replace_projection"
    | "set_entries"
    | "add_entry"
    | "remove_entry"
    | "set_pool"
    | "set_options"
    | "set_alternatives"
    | "select_alternative"
    | "favorite"
    | "next_favorite"
    | "clear_pool"
    | "solve"
    | "import_legacy"
    | "set_mode"
    | "set_locks"
    | "regenerate";
  state?: PlanState;
  entry?: PlanEntry;
  entry_id?: string;
  entries?: PlanEntry[];
  pool?: PlanCourse[];
  sections?: Record<string, unknown[]>;
  empty_days?: PlanDay[];
  avoid_conflicts?: boolean;
  ignore_constraints?: boolean;
  alternatives?: PlanEntry[][];
  alternative_index?: number;
  projection?: Record<string, unknown>;
  what_if?: boolean;
  locked_sections?: Record<string, string>;
};

export type PlanConflict = {
  current: PlanEnvelope;
  message: string;
};

type LegacyDraft = Record<string, unknown>;

type PendingCommand = {
  epoch: number;
  kind: "update" | "undo";
  term: string;
  idempotency_key: string;
  changes?: PlanChanges;
};

const LEGACY_KEYS = ["devrimo:schedule:v1:plan", "devrimo:schedule:v1"];

function isPlanState(value: unknown): value is PlanState {
  return Boolean(value && typeof value === "object" && Array.isArray((value as PlanState).entries));
}

function readLegacyDraft(): LegacyDraft | null {
  for (const key of LEGACY_KEYS) {
    try {
      const raw = window.localStorage.getItem(key);
      if (!raw) continue;
      const value = JSON.parse(raw) as unknown;
      if (value && typeof value === "object") return value as LegacyDraft;
    } catch {
      // A malformed old browser draft is ignored. It is never sent without an
      // explicit import click, so it cannot affect the account on its own.
    }
  }
  return null;
}

function recoveryKey(userId: string, term: string) {
  return `devrimo:schedule:v2:${userId}:${term}`;
}

function readRecoveryState(userId: string, term: string): PlanState | null {
  try {
    const raw = window.localStorage.getItem(recoveryKey(userId, term));
    if (!raw) return null;
    const value = JSON.parse(raw) as unknown;
    if (!value || typeof value !== "object") return null;
    const state = (value as { state?: unknown }).state ?? value;
    return isPlanState(state) ? state : null;
  } catch {
    return null;
  }
}

function legacyDay(value: unknown): PlanDay {
  return value === "Tue" || value === "Wed" || value === "Thu" || value === "Fri" ? value : "Mon";
}

function legacyEntry(value: unknown, index: number, kind: PlanEntry["kind"] = "course"): PlanEntry {
  const item = value && typeof value === "object" ? value as Record<string, unknown> : {};
  const start = Number(item.start_minute ?? item.startMinute ?? (Number(item.start ?? 0) * 60 + 40));
  const duration = Number(item.duration_minutes ?? item.durationMinutes ?? Math.max(1, Number(item.duration ?? 1) * 60 - 10));
  return {
    id: String(item.id ?? `${kind}:${index}`),
    code: String(item.code ?? (kind === "block" ? `BLOCK:${String(item.name ?? "busy")}` : "")),
    name: String(item.name ?? ""),
    section: String(item.section ?? ""),
    credits: Number(item.credits ?? 0),
    color: Number(item.color ?? 0),
    kind: item.kind === "block" ? "block" : kind,
    instructor: String(item.instructor ?? ""),
    day: legacyDay(item.day),
    start_minute: Number.isFinite(start) ? Math.max(0, Math.min(1439, Math.trunc(start))) : 0,
    duration_minutes: Number.isFinite(duration) ? Math.max(1, Math.min(1440, Math.trunc(duration))) : 1,
    room: String(item.room ?? ""),
  };
}

function legacyToState(draft: LegacyDraft): PlanState {
  const isLegacyPayload = draft.state_version === undefined && draft.stateVersion === undefined;
  const explicitWhatIf = typeof draft.what_if === "boolean"
    ? draft.what_if
    : typeof draft.whatIf === "boolean" ? draft.whatIf : null;
  const rawEntries = Array.isArray(draft.entries)
    ? draft.entries
    : [
        ...(Array.isArray(draft.courses) ? draft.courses.flatMap((course) => {
          if (!course || typeof course !== "object") return [];
          const item = course as Record<string, unknown>;
          return (Array.isArray(item.meetings) ? item.meetings : []).map((meeting, index) => ({
            ...(meeting && typeof meeting === "object" ? meeting : {}),
            id: `course:${String(item.code ?? "")}:${String(item.section ?? "")}:${index}`,
            code: item.code,
            name: item.name,
            section: item.section,
            credits: index === 0 ? item.credits : 0,
            instructor: item.instructor,
          }));
        }) : []),
        ...(Array.isArray(draft.busy_blocks) ? draft.busy_blocks.flatMap((block) => {
          if (!block || typeof block !== "object") return [];
          const item = block as Record<string, unknown>;
          return (Array.isArray(item.meetings) ? item.meetings : []).map((meeting, index) => ({
            ...(meeting && typeof meeting === "object" ? meeting : {}),
            id: `block:${String(item.name ?? "busy")}:${index}`,
            code: `BLOCK:${String(item.name ?? "busy")}`,
            name: item.name,
            kind: "block",
          }));
        }) : []),
      ];
  const pool = (Array.isArray(draft.pool) ? draft.pool : []).flatMap((value) => {
    if (!value || typeof value !== "object") return [];
    const item = value as Record<string, unknown>;
    const code = String(item.code ?? "").trim();
    if (!code) return [];
    return [{
      ...item,
      code,
      name: String(item.name ?? code),
      credits: Number(item.credits ?? 0),
      raw_code: String(item.raw_code ?? item.rawCode ?? code),
    }];
  }) as PlanCourse[];
  const alternatives = (Array.isArray(draft.alternatives) ? draft.alternatives : []).map((value) => (
    Array.isArray(value) ? value.map((entry, index) => legacyEntry(entry, index)) : []
  ));
  const favorites = (Array.isArray(draft.favorites) ? draft.favorites : []).map((value) => (
    Array.isArray(value) ? value.map((entry, index) => legacyEntry(entry, index)) : []
  ));
  const emptyDays = (Array.isArray(draft.empty_days) ? draft.empty_days : Array.isArray(draft.emptyDays) ? draft.emptyDays : [])
    .filter((day): day is PlanDay => day === "Mon" || day === "Tue" || day === "Wed" || day === "Thu" || day === "Fri");
  // Old browser drafts contain no signed section evidence. Keep them in an
  // explicitly exploratory branch until the student refreshes sections and
  // saves a new, verified normal plan. A missing mode must never silently
  // become a normal registration-ready write.
  const importedWhatIf = explicitWhatIf ?? isLegacyPayload;
  return {
    entries: rawEntries.map((entry, index) => legacyEntry(entry, index)),
    department: String(draft.department ?? ""),
    department_label: String(draft.department_label ?? draft.departmentLabel ?? ""),
    empty_days: emptyDays,
    avoid_conflicts: typeof draft.avoid_conflicts === "boolean" ? draft.avoid_conflicts : draft.avoidConflicts !== false,
    ignore_constraints: typeof draft.ignore_constraints === "boolean" ? draft.ignore_constraints : (draft.ignoreConstraints === true || importedWhatIf),
    pool,
    sections: (draft.sections && typeof draft.sections === "object" ? draft.sections : {}) as Record<string, unknown[]>,
    alternatives,
    alternative_index: Number(draft.alternative_index ?? draft.alternativeIndex ?? 0),
    favorites,
    favorite_index: Number(draft.favorite_index ?? draft.favoriteIndex ?? (favorites.length ? favorites.length - 1 : -1)),
    what_if: importedWhatIf,
    what_if_backup: draft.what_if_backup && typeof draft.what_if_backup === "object"
      ? draft.what_if_backup as Record<string, unknown>
      : draft.whatIfBackup && typeof draft.whatIfBackup === "object" ? draft.whatIfBackup as Record<string, unknown> : null,
    locked_sections: (draft.locked_sections && typeof draft.locked_sections === "object"
      ? draft.locked_sections
      : draft.lockedSections && typeof draft.lockedSections === "object" ? draft.lockedSections : {}) as Record<string, string>,
    unscheduled_courses: Array.isArray(draft.unscheduled_courses)
      ? draft.unscheduled_courses.filter((value): value is string => typeof value === "string")
      : [],
    generation_error: typeof draft.generation_error === "string" ? draft.generation_error : "",
  };
}

/**
 * Owns loading, optimistic revision ordering, recovery caching and conflict
 * reporting for the server-owned timetable resource.
 */
export function usePlanning(term: string) {
  const [envelope, setEnvelope] = useState<PlanEnvelope | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<PlanConflict | null>(null);
  const [legacyDraft, setLegacyDraft] = useState<LegacyDraft | null>(null);
  const [recoveryDraft, setRecoveryDraft] = useState<PlanState | null>(null);
  const [retryable, setRetryable] = useState(false);
  const queue = useRef<Promise<PlanEnvelope | null>>(Promise.resolve(null));
  const latest = useRef<PlanEnvelope | null>(null);
  const blocked = useRef(false);
  const pendingFailure = useRef<PendingCommand | null>(null);
  const activeTerm = useRef(term);
  const loadSequence = useRef(0);

  // A delayed response from the preceding term must never be adopted into the
  // newly selected term or queued against its revision.
  useEffect(() => {
    activeTerm.current = term;
    latest.current = null;
    blocked.current = false;
    pendingFailure.current = null;
    loadSequence.current += 1;
  }, [term]);

  const adopt = useCallback((next: PlanEnvelope) => {
    latest.current = next;
    setEnvelope(next);
    setSaveError(null);
    setRecoveryDraft(null);
    if (next.user_id && isPlanState(next.state)) {
      try {
        window.localStorage.setItem(recoveryKey(next.user_id, next.term), JSON.stringify(next.state));
      } catch {
        // Recovery is best effort; the server remains authoritative.
      }
    }
  }, []);

  const load = useCallback(async () => {
    const requestedTerm = term;
    const sequence = ++loadSequence.current;
    queue.current = Promise.resolve(null);
    setSaving(false);
    latest.current = null;
    pendingFailure.current = null;
    blocked.current = false;
    setRetryable(false);
    setRecoveryDraft(null);
    setLoading(true);
    setSaveError(null);
    try {
      const next = await jsonFetch<PlanEnvelope>(`/api/schedule/timetable?term=${encodeURIComponent(requestedTerm)}`);
      if (sequence !== loadSequence.current || activeTerm.current !== requestedTerm || next.term !== requestedTerm) return;
      const cached = next.revision === 0 && next.state.entries.length === 0
        ? readRecoveryState(next.user_id, next.term)
        : null;
      adopt(next);
      setConflict(null);
      setRecoveryDraft(cached && JSON.stringify(cached) !== JSON.stringify(next.state) ? cached : null);
      // This read is deliberately the only automatic legacy discovery.  The
      // draft is shown as an import action and never attributed silently.
      if (next.revision === 0 && next.state.entries.length === 0) setLegacyDraft(readLegacyDraft());
      else setLegacyDraft(null);
    } catch (error) {
      if (sequence === loadSequence.current && activeTerm.current === requestedTerm) {
        setSaveError(error instanceof Error ? error.message : "Unable to load the timetable.");
      }
    } finally {
      if (sequence === loadSequence.current && activeTerm.current === requestedTerm) setLoading(false);
    }
  }, [adopt, term]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void load(); }, 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const execute = useCallback(async (command: PendingCommand) => {
    if (command.epoch !== loadSequence.current || activeTerm.current !== command.term || blocked.current && pendingFailure.current !== command) return null;
    const current = latest.current;
    if (!current || current.term !== command.term) return null;
    setSaving(true);
    setSaveError(null);
    try {
      const next = command.kind === "undo"
        ? await jsonFetch<PlanEnvelope>(`/api/schedule/timetable/undo?term=${encodeURIComponent(command.term)}`, {
          method: "POST",
          body: {
            expected_revision: current.revision,
            idempotency_key: command.idempotency_key,
          },
        })
        : await jsonFetch<PlanEnvelope>(`/api/schedule/timetable?term=${encodeURIComponent(command.term)}`, {
          method: "PATCH",
          body: {
            expected_revision: current.revision,
            idempotency_key: command.idempotency_key,
            changes: command.changes,
          },
        });
      if (command.epoch !== loadSequence.current || activeTerm.current !== command.term || next.term !== command.term) return null;
      adopt(next);
      if (pendingFailure.current === command) pendingFailure.current = null;
      setRetryable(false);
      blocked.current = false;
      return next;
    } catch (error) {
      if (command.epoch !== loadSequence.current || activeTerm.current !== command.term) return null;
      if (error instanceof ApiError && error.status === 409) {
        const payload = error.body as { current?: PlanEnvelope; detail?: string } | null;
        const currentPlan = payload?.current;
        if (currentPlan) {
          latest.current = currentPlan;
          setConflict({ current: currentPlan, message: payload?.detail || error.message });
        } else {
          setConflict({ current, message: error.message });
        }
        blocked.current = true;
        pendingFailure.current = null;
        setRetryable(false);
      } else {
        // Keep the exact request key and payload. The server may have committed
        // before the response was lost, so a retry must be an idempotent replay.
        pendingFailure.current = command;
        blocked.current = true;
        setRetryable(true);
      }
      setSaveError(error instanceof Error ? error.message : "Unable to save the timetable.");
      return null;
    } finally {
      if (command.epoch === loadSequence.current && activeTerm.current === command.term) setSaving(false);
    }
  }, [adopt]);

  const enqueue = useCallback((command: PendingCommand) => {
    const task = () => execute(command);
    queue.current = queue.current.then(task, task);
    return queue.current;
  }, [execute]);

  const update = useCallback((changes: PlanChanges, idempotencyKey = crypto.randomUUID()) => {
    if (blocked.current || pendingFailure.current || activeTerm.current !== term) return Promise.resolve(null);
    return enqueue({ epoch: loadSequence.current, kind: "update", term, idempotency_key: idempotencyKey, changes });
  }, [enqueue, term]);

  const undo = useCallback((idempotencyKey = crypto.randomUUID()) => {
    if (blocked.current || pendingFailure.current || activeTerm.current !== term) return Promise.resolve(null);
    return enqueue({ epoch: loadSequence.current, kind: "undo", term, idempotency_key: idempotencyKey });
  }, [enqueue, term]);

  const retry = useCallback(() => {
    const command = pendingFailure.current;
    if (!command || activeTerm.current !== command.term) return Promise.resolve(null);
    blocked.current = false;
    return enqueue(command);
  }, [enqueue]);

  const importLegacy = useCallback(async (idempotencyKey?: string) => {
    if (!legacyDraft) return null;
    const result = await update({ operation: "import_legacy", state: legacyToState(legacyDraft) }, idempotencyKey);
    if (result) setLegacyDraft(null);
    return result;
  }, [legacyDraft, update]);

  const refreshAfterConflict = useCallback(async () => {
    blocked.current = false;
    setConflict(null);
    await load();
  }, [load]);

  const restoreRecovery = useCallback((idempotencyKey?: string) => {
    if (!recoveryDraft) return Promise.resolve(null);
    return update({ operation: "replace", state: recoveryDraft }, idempotencyKey);
  }, [recoveryDraft, update]);

  return {
    envelope,
    state: envelope?.state ?? null,
    revision: envelope?.revision ?? 0,
    ready: !loading && envelope !== null && envelope.term === term,
    loading,
    saving,
    saveError,
    conflict,
    legacyDraft,
    recoveryDraft,
    restoreRecovery,
    retryable,
    retry,
    update,
    undo,
    importLegacy,
    refreshAfterConflict,
  };
}
