export function normalizePlannerCourseIdentity(value: unknown): string {
  return typeof value === "string" ? value.toUpperCase().replace(/[^A-Z0-9]/g, "") : "";
}

/** Exact display and catalog identities for one selected course. */
export function plannerCourseAliases(course: { code?: unknown; rawCode?: unknown; raw_code?: unknown }): Set<string> {
  return new Set(
    [course.code, course.rawCode, course.raw_code]
      .map(normalizePlannerCourseIdentity)
      .filter(Boolean),
  );
}

export function plannerCourseAliasMatches(value: unknown, aliases: Set<string>): boolean {
  const identity = normalizePlannerCourseIdentity(value);
  return Boolean(identity) && aliases.has(identity);
}
