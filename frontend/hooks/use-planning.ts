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
  /** A section added while catalog verification was incomplete. */
  tentative?: boolean;
  verification_status?: "verified" | "tentative" | string;
  catalog_release_id?: string | null;
};

export type PlanCourse = {
  code: string;
  name: string;
  credits: number;
  raw_code?: string | null;
  rawCode?: string;
  /** Selected by a credit plan even when it has no calendar meetings. */
  selected?: boolean;
  timing_status?: string | null;
  selected_section?: string | null;
  [key: string]: unknown;
};

export type PlanState = {
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
  catalog_release_id?: string | null;
  academic_snapshot_fetched_at?: string | null;
  needs_revalidation?: boolean;
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
  catalog_release_id?: string | null;
  needs_revalidation?: boolean;
};

export type PlanChanges = {
  operation:
    | "replace"
    | "replace_projection"
    | "set_entries"
    | "apply_proposal"
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
    | "import_legacy";
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

export function isRetryablePlanningFailure(status: number | null): boolean {
  return status === null || status === 408 || status === 429 || status >= 500;
}

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
      } else if (error instanceof ApiError && !isRetryablePlanningFailure(error.status)) {
        // Validation and authorization failures did not commit. Replaying the
        // same payload can never fix them, and locking the queue here prevents
        // the student from removing the invalid section or clearing the plan.
        pendingFailure.current = null;
        blocked.current = false;
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

  /** Apply a server-generated plan proposal through the same revision queue. */
  const applyProposal = useCallback((changes: PlanChanges, idempotencyKey = crypto.randomUUID()) => {
    if (changes.operation !== "apply_proposal") {
      return Promise.reject(new Error("A proposal application must use apply_proposal."));
    }
    return update(changes, idempotencyKey);
  }, [update]);

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
    const result = await update({ operation: "import_legacy", projection: legacyDraft }, idempotencyKey);
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
    applyProposal,
    undo,
    importLegacy,
    refreshAfterConflict,
  };
}
