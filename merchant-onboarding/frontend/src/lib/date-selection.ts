// ─────────────────────────────────────────────────────────────────────────
// DateSelection — the value model passed between DaysRangePicker and its
// consumers. Three shapes:
//   • { kind: "days",  days }       — last N days ending today (preset / N-days input)
//   • { kind: "range", start, end } — explicit inclusive ISO date range
//   • { kind: "all"  }              — no filter
//
// Dates are kept as "YYYY-MM-DD" strings (not Date objects) to avoid TZ
// surprises when they end up in URLs and React Query keys.
// ─────────────────────────────────────────────────────────────────────────

export type DateSelection =
  | { kind: "days"; days: number }
  | { kind: "range"; start: string; end: string }
  | { kind: "all" };

export const ALL_TIME: DateSelection = { kind: "all" };

/** "2026-05-22"-style string for a Date. Local-TZ date components (avoids the
 *  UTC-shift gotcha that `toISOString()` introduces). */
export function toIsoDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}

/** Parse "YYYY-MM-DD" into a local-TZ Date. Returns null on bad input. */
export function fromIsoDate(s: string | undefined | null): Date | null {
  if (!s) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (!m) return null;
  const [, y, mo, d] = m;
  const dt = new Date(Number(y), Number(mo) - 1, Number(d));
  return Number.isNaN(dt.getTime()) ? null : dt;
}

/** Convert a DateSelection into the API query parameters the backend
 *  understands. Range wins over days; all-time sends nothing. */
export function selectionToApiParams(
  s: DateSelection,
): { days?: number; start_date?: string; end_date?: string } {
  if (s.kind === "range") return { start_date: s.start, end_date: s.end };
  if (s.kind === "days")  return { days: s.days };
  return {};
}

/** Short human label for the title rail ("last 7 days", "Jan 1 – Jan 14, 2026", "all time"). */
export function selectionLabel(s: DateSelection): string {
  if (s.kind === "all") return "all time";
  if (s.kind === "days") return `last ${s.days} day${s.days === 1 ? "" : "s"}`;
  return formatRangeLabel(s.start, s.end);
}

function formatRangeLabel(startIso: string, endIso: string): string {
  const s = fromIsoDate(startIso);
  const e = fromIsoDate(endIso);
  if (!s || !e) return `${startIso} – ${endIso}`;
  const fmt = (d: Date) =>
    d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const sameYear = s.getFullYear() === e.getFullYear();
  if (sameYear) return `${fmt(s)} – ${fmt(e)}, ${e.getFullYear()}`;
  return `${fmt(s)} ${s.getFullYear()} – ${fmt(e)} ${e.getFullYear()}`;
}
