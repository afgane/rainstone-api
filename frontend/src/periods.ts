/**
 * Calendar periods in the reporting timezone.
 *
 * "Yesterday" is the preceding calendar day, "last week" the previous Monday to
 * Sunday, "last month" the previous calendar month — never a rolling 7 or 30
 * day window. Users pick inclusive dates; the API receives an exclusive end.
 */

export type PeriodId =
  | "today"
  | "yesterday"
  | "this-week"
  | "last-week"
  | "this-month"
  | "last-month"
  | "custom";

export interface Period {
  id: PeriodId;
  /** Inclusive first day, as a calendar date in the reporting timezone. */
  fromDate: string;
  /** Inclusive last day, as shown to the user. */
  toDate: string;
  /** True while the period is still running, so totals keep growing. */
  open: boolean;
}

export const PERIOD_LABELS: Record<PeriodId, string> = {
  today: "Today",
  yesterday: "Yesterday",
  "this-week": "This week",
  "last-week": "Last week",
  "this-month": "This month",
  "last-month": "Last month",
  custom: "Custom dates",
};

export const PERIOD_ORDER: PeriodId[] = [
  "today",
  "yesterday",
  "this-week",
  "last-week",
  "this-month",
  "last-month",
  "custom",
];

function parts(instant: Date, timezone: string): { year: number; month: number; day: number } {
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  const [year, month, day] = formatter.format(instant).split("-").map(Number);
  return { year, month, day };
}

function calendarDate(year: number, month: number, day: number): string {
  return `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

/** Today's calendar date in the reporting timezone. */
export function todayIn(timezone: string, now = new Date()): string {
  const { year, month, day } = parts(now, timezone);
  return calendarDate(year, month, day);
}

/** Calendar arithmetic on a date string, free of timezone drift. */
export function shiftDays(date: string, days: number): string {
  const [year, month, day] = date.split("-").map(Number);
  const moved = new Date(Date.UTC(year, month - 1, day + days));
  return calendarDate(moved.getUTCFullYear(), moved.getUTCMonth() + 1, moved.getUTCDate());
}

/** Monday of the calendar week containing this date. */
export function weekStart(date: string): string {
  const [year, month, day] = date.split("-").map(Number);
  const weekday = new Date(Date.UTC(year, month - 1, day)).getUTCDay();
  const mondayOffset = weekday === 0 ? -6 : 1 - weekday;
  return shiftDays(date, mondayOffset);
}

export function monthStart(date: string): string {
  const [year, month] = date.split("-").map(Number);
  return calendarDate(year, month, 1);
}

export function monthEnd(date: string): string {
  const [year, month] = date.split("-").map(Number);
  const nextMonth = new Date(Date.UTC(year, month, 1));
  return shiftDays(
    calendarDate(nextMonth.getUTCFullYear(), nextMonth.getUTCMonth() + 1, 1),
    -1,
  );
}

export function resolvePeriod(
  id: PeriodId,
  timezone: string,
  custom?: { fromDate: string; toDate: string },
  now = new Date(),
): Period {
  const today = todayIn(timezone, now);
  switch (id) {
    case "today":
      return { id, fromDate: today, toDate: today, open: true };
    case "yesterday": {
      const day = shiftDays(today, -1);
      return { id, fromDate: day, toDate: day, open: false };
    }
    case "this-week":
      return { id, fromDate: weekStart(today), toDate: today, open: true };
    case "last-week": {
      const start = shiftDays(weekStart(today), -7);
      return { id, fromDate: start, toDate: shiftDays(start, 6), open: false };
    }
    case "last-month": {
      const start = monthStart(shiftDays(monthStart(today), -1));
      return { id, fromDate: start, toDate: monthEnd(start), open: false };
    }
    case "custom":
      return {
        id,
        fromDate: custom?.fromDate || monthStart(today),
        toDate: custom?.toDate || today,
        open: false,
      };
    case "this-month":
    default:
      return { id: "this-month", fromDate: monthStart(today), toDate: today, open: true };
  }
}

const DISPLAY = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});

function readable(date: string): string {
  const [year, month, day] = date.split("-").map(Number);
  return DISPLAY.format(new Date(Date.UTC(year, month - 1, day)));
}

/** The resolved dates, so a period label is never ambiguous. */
export function describePeriod(period: Period, timezone: string): string {
  const range =
    period.fromDate === period.toDate
      ? readable(period.fromDate)
      : `${readable(period.fromDate)} – ${readable(period.toDate)}`;
  return `${range}${period.open ? " (so far)" : ""} · ${timezone}`;
}

/** The API boundary is exclusive; users never type "to, exclusive". */
export function exclusiveEnd(period: Period): string {
  return shiftDays(period.toDate, 1);
}
