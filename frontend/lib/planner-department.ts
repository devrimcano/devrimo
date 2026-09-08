export type PlannerDepartmentChoice = {
  code: string;
  label: string;
};

export type ResolvedPlannerDepartment = PlannerDepartmentChoice & {
  source: "student-context" | "fallback" | "none";
};

function normalized(choice: PlannerDepartmentChoice | null | undefined): PlannerDepartmentChoice | null {
  const code = choice?.code.trim() ?? "";
  if (!code) return null;
  return { code, label: choice?.label.trim() || code };
}

/** Keep the verified SAIS identity authoritative regardless of response order. */
export function resolvePlannerDepartment(
  studentContext: PlannerDepartmentChoice | null | undefined,
  fallback: PlannerDepartmentChoice | null | undefined,
): ResolvedPlannerDepartment {
  const student = normalized(studentContext);
  if (student) return { ...student, source: "student-context" };
  const savedFallback = normalized(fallback);
  if (savedFallback) return { ...savedFallback, source: "fallback" };
  return { code: "", label: "", source: "none" };
}
