export type PlannerDay = "Mon" | "Tue" | "Wed" | "Thu" | "Fri";

export const PLANNER_DAYS: readonly PlannerDay[] = ["Mon", "Tue", "Wed", "Thu", "Fri"];

export const DAY_INDEX: Record<PlannerDay, number> = {
  Mon: 0,
  Tue: 1,
  Wed: 2,
  Thu: 3,
  Fri: 4,
};

export type PlannerCalendarEntry = {
  id: string;
  code: string;
  name: string;
  section: string;
  day: PlannerDay;
  startMinute?: number;
  durationMinutes?: number;
  start: number;
  duration: number;
  room: string;
  instructor?: string;
  kind: "course" | "block";
};

export type PlannerCalendarBounds = {
  minMinute: number;
  maxMinute: number;
  minTime: string;
  maxTime: string;
  scrollTime: string;
};

/**
 * Keep support for the hour-shaped fields used by older saved planner data.
 * New canonical entries use the minute fields, which preserve exact meeting
 * times instead of rounding them to an hour.
 */
export function entryStartMinute(entry: Pick<PlannerCalendarEntry, "start" | "startMinute">): number {
  const value = entry.startMinute ?? entry.start * 60 + 40;
  return Number.isFinite(value) ? Math.max(0, value) : 0;
}

export function entryDurationMinutes(entry: Pick<PlannerCalendarEntry, "duration" | "durationMinutes">): number {
  const value = entry.durationMinutes ?? Math.max(1, entry.duration * 60 - 10);
  return Number.isFinite(value) ? Math.max(1, value) : 1;
}

export function minutesToDuration(minutes: number): string {
  const safeMinutes = Math.max(0, Math.round(minutes));
  const hours = Math.floor(safeMinutes / 60);
  const remainder = safeMinutes % 60;
  return `${String(hours).padStart(2, "0")}:${String(remainder).padStart(2, "0")}:00`;
}

export function calendarBounds(entries: readonly PlannerCalendarEntry[]): PlannerCalendarBounds {
  if (!entries.length) {
    const minMinute = 8 * 60;
    const maxMinute = 18 * 60;
    return {
      minMinute,
      maxMinute,
      minTime: minutesToDuration(minMinute),
      maxTime: minutesToDuration(maxMinute),
      scrollTime: minutesToDuration(minMinute),
    };
  }

  const earliest = Math.min(...entries.map((entry) => entryStartMinute(entry)));
  const latest = Math.max(...entries.map((entry) => entryStartMinute(entry) + entryDurationMinutes(entry)));
  const minMinute = Math.max(0, Math.floor(earliest / 30) * 30);
  const maxMinute = Math.max(minMinute + 30, Math.ceil(latest / 30) * 30);

  return {
    minMinute,
    maxMinute,
    minTime: minutesToDuration(minMinute),
    maxTime: minutesToDuration(maxMinute),
    scrollTime: minutesToDuration(minMinute),
  };
}

export function dateForEntry(entry: Pick<PlannerCalendarEntry, "day" | "start" | "startMinute">): Date {
  const date = new Date(Date.UTC(2024, 0, 1 + DAY_INDEX[entry.day]));
  const startMinute = entryStartMinute(entry);
  date.setUTCHours(Math.floor(startMinute / 60), Math.round(startMinute % 60), 0, 0);
  return date;
}

export function plannerDayFromDate(date: Date): PlannerDay | null {
  switch (date.getUTCDay()) {
    case 1:
      return "Mon";
    case 2:
      return "Tue";
    case 3:
      return "Wed";
    case 4:
      return "Thu";
    case 5:
      return "Fri";
    default:
      return null;
  }
}

export function dayLabel(day: PlannerDay, locale: "tr" | "en"): string {
  const labels = locale === "tr"
    ? { Mon: "Pzt", Tue: "Sal", Wed: "Çar", Thu: "Per", Fri: "Cum" }
    : { Mon: "Mon", Tue: "Tue", Wed: "Wed", Thu: "Thu", Fri: "Fri" };
  return labels[day];
}

export function formatMinute(minute: number): string {
  const safeMinute = Math.max(0, Math.round(minute));
  const hours = Math.floor(safeMinute / 60) % 24;
  const remainder = safeMinute % 60;
  return `${String(hours).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
}
