export type PlannerIntentStep = "details" | "courses" | "timetable";

export type PlannerStepAvailability = Record<PlannerIntentStep, boolean>;

/**
 * Resolve a URL step only after the saved canonical plan is ready to be
 * reflected in the controls. Returning null keeps the intent pending during
 * the short hydration window instead of downgrading a saved timetable.
 */
export function resolvePlannerIntentStep(
  requested: PlannerIntentStep,
  canOpen: PlannerStepAvailability,
  planningReady: boolean,
  hydrated: boolean,
): PlannerIntentStep | null {
  if (!planningReady || !hydrated) return null;
  if (canOpen[requested]) return requested;
  return requested === "timetable" && canOpen.courses ? "courses" : "details";
}
