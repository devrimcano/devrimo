"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import FullCalendar from "@fullcalendar/react";
import interactionPlugin from "@fullcalendar/interaction";
import timeGridPlugin from "@fullcalendar/timegrid";
import type { EventClickArg, EventDropArg, EventMountArg } from "@fullcalendar/core";
import type { EventResizeDoneArg } from "@fullcalendar/interaction";
import {
  PLANNER_DAYS,
  calendarBounds,
  dateForEntry,
  dayLabel,
  entryDurationMinutes,
  entryStartMinute,
  formatMinute,
  plannerDayFromDate,
  type PlannerCalendarEntry,
  type PlannerDay,
} from "./planner-calendar-logic";

export type { PlannerCalendarEntry } from "./planner-calendar-logic";

export type PlannerCalendarLocale = "tr" | "en";

export type PlannerCalendarBlockChange = {
  id: string;
  day: PlannerDay;
  startMinute: number;
  durationMinutes: number;
};

export type PlannerCalendarLabels = {
  week: string;
  room: string;
  noRoom: string;
  noEntries?: string;
  selectDay?: string;
  selectEntry?: string;
  course?: string;
  block?: string;
  moveEarlier?: string;
  moveLater?: string;
  shorten?: string;
  lengthen?: string;
  /** Kept for source compatibility while the parent wires the details drawer. */
  removeCourse?: string;
};

export type PlannerCalendarProps = {
  entries: PlannerCalendarEntry[];
  onSelectEntry: (entry: PlannerCalendarEntry) => void;
  onChangeBlock?: (change: PlannerCalendarBlockChange) => void;
  locale?: PlannerCalendarLocale;
  labels?: PlannerCalendarLabels;
  /** @deprecated Course selection must never be destructive from the calendar. */
  onRemoveCourse?: (code: string, section: string) => void;
};

const BASE_DATE = new Date(Date.UTC(2024, 0, 1));

function defaultsFor(locale: PlannerCalendarLocale): Required<Pick<PlannerCalendarLabels, "noEntries" | "selectDay" | "selectEntry" | "course" | "block" | "moveEarlier" | "moveLater" | "shorten" | "lengthen">> {
  return locale === "tr"
    ? {
        noEntries: "Bu gün için program yok.",
        selectDay: "Program gününü seç",
        selectEntry: "Ders ayrıntılarını aç",
        course: "Ders",
        block: "Kişisel blok",
        moveEarlier: "Kişisel bloğu erkene al",
        moveLater: "Kişisel bloğu ileri al",
        shorten: "Kişisel bloğu kısalt",
        lengthen: "Kişisel bloğu uzat",
      }
    : {
        noEntries: "No schedule entries for this day.",
        selectDay: "Choose a schedule day",
        selectEntry: "Open class details",
        course: "Course",
        block: "Personal block",
        moveEarlier: "Move personal block earlier",
        moveLater: "Move personal block later",
        shorten: "Shorten personal block",
        lengthen: "Lengthen personal block",
      };
}

function eventEntry(info: { event: { extendedProps: Record<string, unknown> } }): PlannerCalendarEntry | undefined {
  const entry = info.event.extendedProps.entry;
  return entry && typeof entry === "object" ? entry as PlannerCalendarEntry : undefined;
}

function eventTitle(entry: PlannerCalendarEntry): string {
  return `${entry.code}${entry.section ? ` · ${entry.section}` : ""}`;
}

function eventMinutes(event: { start: Date | null; end: Date | null }, fallback: PlannerCalendarEntry): { startMinute: number; durationMinutes: number } | null {
  if (!event.start) return null;
  const startMinute = event.start.getUTCHours() * 60 + event.start.getUTCMinutes();
  const durationMinutes = event.end
    ? Math.max(1, Math.round((event.end.getTime() - event.start.getTime()) / 60_000))
    : entryDurationMinutes(fallback);
  return { startMinute, durationMinutes };
}

function eventLabel(entry: PlannerCalendarEntry, labels: Required<Pick<PlannerCalendarLabels, "room" | "noRoom" | "selectEntry" | "course" | "block">>): string {
  const kind = entry.kind === "block" ? labels.block : labels.course;
  const room = entry.room.trim() || labels.noRoom;
  const section = entry.section ? ` · ${entry.section}` : "";
  return `${kind}: ${entry.code}${section} · ${entry.name} · ${labels.room}: ${room}. ${labels.selectEntry}`;
}

function shiftBlock(entry: PlannerCalendarEntry, delta: number): PlannerCalendarBlockChange {
  const startMinute = entryStartMinute(entry);
  const durationMinutes = entryDurationMinutes(entry);
  const nextStart = Math.min(Math.max(0, startMinute + delta), Math.max(0, 24 * 60 - durationMinutes));
  return { id: entry.id, day: entry.day, startMinute: nextStart, durationMinutes };
}

function resizeBlock(entry: PlannerCalendarEntry, delta: number): PlannerCalendarBlockChange {
  const startMinute = entryStartMinute(entry);
  const durationMinutes = Math.min(Math.max(1, entryDurationMinutes(entry) + delta), Math.max(1, 24 * 60 - startMinute));
  return { id: entry.id, day: entry.day, startMinute, durationMinutes };
}

export function PlannerCalendar({
  entries,
  onSelectEntry,
  onChangeBlock,
  locale = "en",
  labels,
}: PlannerCalendarProps) {
  const defaults = useMemo(() => defaultsFor(locale), [locale]);
  const text = useMemo(() => ({
    ...defaults,
    week: labels?.week ?? (locale === "tr" ? "Haftalık program" : "Weekly schedule"),
    room: labels?.room ?? (locale === "tr" ? "Derslik" : "Room"),
    noRoom: labels?.noRoom ?? "TBA",
    noEntries: labels?.noEntries ?? defaults.noEntries,
    selectDay: labels?.selectDay ?? defaults.selectDay,
    selectEntry: labels?.selectEntry ?? defaults.selectEntry,
    course: labels?.course ?? defaults.course,
    block: labels?.block ?? defaults.block,
    moveEarlier: labels?.moveEarlier ?? defaults.moveEarlier,
    moveLater: labels?.moveLater ?? defaults.moveLater,
    shorten: labels?.shorten ?? defaults.shorten,
    lengthen: labels?.lengthen ?? defaults.lengthen,
  }), [defaults, labels, locale]);
  const bounds = useMemo(() => calendarBounds(entries), [entries]);
  const [mobileDay, setMobileDay] = useState<PlannerDay>(entries[0]?.day ?? "Mon");
  const mobileDayWasSelected = useRef(false);
  const dayButtons = useRef(new Map<PlannerDay, HTMLButtonElement>());
  const keyHandlers = useRef(new WeakMap<HTMLElement, (event: KeyboardEvent) => void>());

  useEffect(() => {
    if (!mobileDayWasSelected.current && entries.length && !entries.some((entry) => entry.day === mobileDay)) {
      setMobileDay(entries[0].day);
    }
  }, [entries, mobileDay]);

  const selectEntry = (entry: PlannerCalendarEntry) => {
    onSelectEntry(entry);
  };

  const events = useMemo(() => entries.map((entry) => {
    const start = dateForEntry(entry);
    const end = new Date(start.getTime() + entryDurationMinutes(entry) * 60_000);
    const editable = entry.kind === "block" && Boolean(onChangeBlock);
    return {
      id: entry.id,
      title: eventTitle(entry),
      start,
      end,
      editable,
      startEditable: editable,
      durationEditable: editable,
      backgroundColor: entry.kind === "block" ? "var(--muted-foreground)" : "var(--primary)",
      borderColor: entry.kind === "block" ? "var(--muted-foreground)" : "var(--primary)",
      textColor: entry.kind === "block" ? "var(--foreground)" : "var(--primary-foreground)",
      extendedProps: { entry },
    };
  }), [entries, onChangeBlock]);

  const handleClick = (info: EventClickArg) => {
    const entry = eventEntry(info);
    if (entry) selectEntry(entry);
  };

  const handleDrop = (info: EventDropArg) => {
    const entry = eventEntry(info);
    const timing = entry && eventMinutes(info.event, entry);
    const day = info.event.start ? plannerDayFromDate(info.event.start) : null;
    if (!entry || entry.kind !== "block" || !timing || !day || !onChangeBlock) {
      info.revert();
      return;
    }
    onChangeBlock({ id: entry.id, day, ...timing });
  };

  const handleResize = (info: EventResizeDoneArg) => {
    const entry = eventEntry(info);
    const timing = entry && eventMinutes(info.event, entry);
    const day = info.event.start ? plannerDayFromDate(info.event.start) : null;
    if (!entry || entry.kind !== "block" || !timing || !day || !onChangeBlock) {
      info.revert();
      return;
    }
    onChangeBlock({ id: entry.id, day, ...timing });
  };

  const handleMount = (info: EventMountArg) => {
    const entry = eventEntry(info);
    if (!entry) return;
    const element = info.el;
    element.tabIndex = 0;
    element.setAttribute("role", "button");
    const label = eventLabel(entry, text);
    element.setAttribute("aria-label", label);
    element.title = label;
    element.classList.add("focus-visible:outline-none", "focus-visible:ring-2", "focus-visible:ring-ring");
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      selectEntry(entry);
    };
    element.addEventListener("keydown", handleKeyDown);
    keyHandlers.current.set(element, handleKeyDown);
  };

  const handleUnmount = (info: EventMountArg) => {
    const handler = keyHandlers.current.get(info.el);
    if (handler) info.el.removeEventListener("keydown", handler);
    keyHandlers.current.delete(info.el);
  };

  const mobileEntries = useMemo(
    () => entries
      .filter((entry) => entry.day === mobileDay)
      .slice()
      .sort((left, right) => entryStartMinute(left) - entryStartMinute(right) || left.code.localeCompare(right.code)),
    [entries, mobileDay],
  );

  const chooseMobileDay = (day: PlannerDay, moveFocus = false) => {
    mobileDayWasSelected.current = true;
    setMobileDay(day);
    if (moveFocus) dayButtons.current.get(day)?.focus();
  };

  const handleDayKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>, day: PlannerDay) => {
    const index = PLANNER_DAYS.indexOf(day);
    let nextIndex = -1;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex = (index + 1) % PLANNER_DAYS.length;
    if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextIndex = (index - 1 + PLANNER_DAYS.length) % PLANNER_DAYS.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = PLANNER_DAYS.length - 1;
    if (nextIndex < 0) return;
    event.preventDefault();
    chooseMobileDay(PLANNER_DAYS[nextIndex], true);
  };

  return (
    <div className="planner-calendar w-full min-w-0" aria-label={text.week}>
      <div className="hidden min-w-0 md:block" data-planner-week>
        <FullCalendar
          key={locale}
          plugins={[timeGridPlugin, interactionPlugin]}
          initialView="timeGridWeek"
          initialDate={BASE_DATE}
          timeZone="UTC"
          firstDay={1}
          weekends={false}
          allDaySlot={false}
          slotMinTime={bounds.minTime}
          slotMaxTime={bounds.maxTime}
          slotDuration="00:30:00"
          scrollTime={bounds.scrollTime}
          height="auto"
          expandRows
          nowIndicator={false}
          editable={Boolean(onChangeBlock)}
          selectable={false}
          eventClick={handleClick}
          eventDrop={handleDrop}
          eventResize={handleResize}
          events={events}
          headerToolbar={false}
          dayHeaderContent={(arg) => {
            const day = plannerDayFromDate(arg.date);
            return day ? dayLabel(day, locale) : arg.text;
          }}
          slotLabelFormat={{ hour: "2-digit", minute: "2-digit", hour12: false }}
          eventContent={(info) => {
            const entry = eventEntry(info);
            if (!entry) return info.event.title;
            return (
              <span className="block overflow-hidden p-1 text-xs leading-tight">
                <strong className="block truncate">{info.event.title}</strong>
                <span className="block truncate">{entry.room.trim() || text.noRoom}</span>
                <span className="block truncate opacity-80">{entry.name}</span>
              </span>
            );
          }}
          eventDidMount={handleMount}
          eventWillUnmount={handleUnmount}
        />
      </div>

      <div className="md:hidden" data-planner-agenda>
        <div className="mb-3 grid grid-cols-5 gap-1" role="tablist" aria-label={text.selectDay}>
          {PLANNER_DAYS.map((day) => {
            const selected = mobileDay === day;
            const tabId = `planner-day-${day.toLowerCase()}`;
            return (
              <button
                key={day}
                id={tabId}
                type="button"
                role="tab"
                aria-selected={selected}
                aria-controls="planner-agenda-panel"
                tabIndex={selected ? 0 : -1}
                ref={(element) => {
                  if (element) dayButtons.current.set(day, element);
                  else dayButtons.current.delete(day);
                }}
                onClick={() => chooseMobileDay(day)}
                onKeyDown={(event) => handleDayKeyDown(event, day)}
                className={`h-9 rounded-md border text-xs font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${selected ? "border-primary bg-primary text-primary-foreground" : "text-muted-foreground"}`}
              >
                {dayLabel(day, locale)}
              </button>
            );
          })}
        </div>

        <div id="planner-agenda-panel" role="tabpanel" aria-labelledby={`planner-day-${mobileDay.toLowerCase()}`}>
          {mobileEntries.length ? (
            <ol className="space-y-2">
              {mobileEntries.map((entry) => {
                const startMinute = entryStartMinute(entry);
                const durationMinutes = entryDurationMinutes(entry);
                const room = entry.room.trim() || text.noRoom;
                const label = eventLabel(entry, text);
                const canMoveEarlier = startMinute > 0;
                const canMoveLater = startMinute + durationMinutes < 24 * 60;
                const canShorten = durationMinutes > 1;
                const canLengthen = startMinute + durationMinutes < 24 * 60;
                return (
                  <li key={entry.id} className="rounded-xl border bg-card/60 p-3">
                    <button
                      type="button"
                      onClick={() => selectEntry(entry)}
                      className="flex w-full items-start gap-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      aria-label={label}
                    >
                      <span className="w-24 shrink-0 text-sm font-semibold tabular-nums">
                        {formatMinute(startMinute)}–{formatMinute(startMinute + durationMinutes)}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block font-semibold">{entry.kind === "block" ? text.block : eventTitle(entry)}</span>
                        <span className="mt-0.5 block text-xs leading-snug opacity-85">{entry.kind === "block" ? entry.name : `${entry.name} · ${text.room}: ${room}`}</span>
                      </span>
                    </button>
                    {entry.kind === "block" && onChangeBlock ? (
                      <div className="mt-3 flex flex-wrap gap-2 border-t pt-2" aria-label={text.block}>
                        <button type="button" className="rounded-md border px-2 py-1 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" disabled={!canMoveEarlier} onClick={() => onChangeBlock(shiftBlock(entry, -30))}>{text.moveEarlier}</button>
                        <button type="button" className="rounded-md border px-2 py-1 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" disabled={!canMoveLater} onClick={() => onChangeBlock(shiftBlock(entry, 30))}>{text.moveLater}</button>
                        <button type="button" className="rounded-md border px-2 py-1 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" disabled={!canShorten} onClick={() => onChangeBlock(resizeBlock(entry, -30))}>{text.shorten}</button>
                        <button type="button" className="rounded-md border px-2 py-1 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" disabled={!canLengthen} onClick={() => onChangeBlock(resizeBlock(entry, 30))}>{text.lengthen}</button>
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ol>
          ) : (
            <p className="rounded-xl border border-dashed p-4 text-sm text-muted-foreground">{text.noEntries}</p>
          )}
        </div>
      </div>
    </div>
  );
}
