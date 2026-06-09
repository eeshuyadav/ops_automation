import { format, parseISO } from "date-fns";

// ─────────────────────────────────────────────────────────────────────────
// Shared formatting helpers
//
// These were previously duplicated as local helpers inside SpeedPanel,
// EbTimePanel, TrendChart, and the page components. Centralising them here
// so the whole app speaks the same human-readable language.
//
// Every function is defensive: it handles null / undefined / NaN / empty
// strings and never throws on bad input.
// ─────────────────────────────────────────────────────────────────────────

const EMPTY = "—";

/**
 * Render a day-count in long form.
 *
 *   0   → "Same day"
 *   1   → "1 day"
 *   2.5 → "2.5 days"
 *   null/undefined → "—"
 */
export function fmtDays(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return EMPTY;
  if (n === 0) return "Same day";
  if (Number.isInteger(n)) return `${n} ${n === 1 ? "day" : "days"}`;
  return `${n.toFixed(1)} days`;
}

/**
 * Compact day-count for tight UI (chips, axes, badges).
 *
 *   0   → "0d"
 *   1   → "1d"
 *   2.5 → "2.5d"
 *   null/undefined → "—"
 */
export function fmtDaysShort(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return EMPTY;
  if (Number.isInteger(n)) return `${n}d`;
  return `${n.toFixed(1)}d`;
}

/**
 * Parse an ISO date string into the short "d MMM" form (e.g. "20 Apr").
 *
 * - null / undefined / empty string → "—"
 * - unparseable input → the raw string echoed back unchanged
 *   (the upstream sheet sometimes stores free-text dates)
 */
export function fmtDate(raw: string | null | undefined): string {
  if (!raw) return EMPTY;
  try {
    const d = parseISO(raw);
    if (!Number.isNaN(d.getTime())) return format(d, "d MMM");
  } catch {
    /* fall through to raw */
  }
  return raw;
}

/**
 * Render a date window like "20 Apr – 20 May" from two ISO date strings.
 * If either side fails to parse we return "{start} – {end}" verbatim so the
 * caller still sees something useful.
 */
export function fmtWindow(startISO: string, endISO: string): string {
  try {
    return `${format(parseISO(startISO), "d MMM")} – ${format(parseISO(endISO), "d MMM")}`;
  } catch {
    return `${startISO} – ${endISO}`;
  }
}

/**
 * Integer percentage: `Math.round((n / total) * 100)`.
 * Returns 0 when `total` is 0 or negative (avoids NaN / Infinity).
 */
export function pct(n: number, total: number): number {
  if (!total || total <= 0) return 0;
  return Math.round((n / total) * 100);
}

/**
 * Human label for a days-window filter.
 *
 *   undefined → "all time"
 *   7         → "last 7 days"
 *   1         → "last 1 day"
 */
export function fmtWindowLabel(days: number | undefined): string {
  if (days === undefined) return "all time";
  return `last ${days} day${days === 1 ? "" : "s"}`;
}
