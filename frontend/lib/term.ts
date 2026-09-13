/**
 * The one term a student can be registering for, derived from the calendar.
 *
 * METU term codes are a four-digit year plus a part number, where the year is
 * the one the academic year starts in: 20261 is 2026-2027 Fall, and 20253 is
 * the summer school that runs during calendar 2026. There is exactly one term
 * a student can be registering for at any point in the year, so this is
 * derived and displayed rather than offered as a choice.
 *
 * Lives here rather than inside the planner because the settings page had a
 * hardcoded default that drifted from it.
 */
export function upcomingTerm(now: Date = new Date()) {
  const year = now.getFullYear();
  const month = now.getMonth() + 1;
  if (month >= 8) return `${year}1`;
  if (month <= 5) return `${year - 1}2`;
  return `${year - 1}3`;
}
