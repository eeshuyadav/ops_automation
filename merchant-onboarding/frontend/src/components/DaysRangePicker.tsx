import { useState } from "react";
import { CalendarRange } from "lucide-react";
import type { DateRange } from "react-day-picker";

import { Input } from "@/components/ui/input";
import { Calendar } from "@/components/ui/calendar";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import {
  type DateSelection,
  fromIsoDate,
  toIsoDate,
} from "@/lib/date-selection";

// ─────────────────────────────────────────────────────────────────────────
// DaysRangePicker
//
// Picks a window for filtered views. Three modes:
//   • quick-range buttons (7d / 14d / 30d)
//   • custom N-days numeric input + Apply
//   • a Custom Range popover with a 2-up calendar (DayPicker mode="range")
//   • optional All-time toggle on the right
//
// The control is fully controlled — parent owns the `value: DateSelection`
// and reacts to `onChange`. See @/lib/date-selection for the union type.
// ─────────────────────────────────────────────────────────────────────────

interface Props {
  /** Currently-selected window. */
  value: DateSelection;
  /** Called whenever any sub-control changes selection. */
  onChange: (v: DateSelection) => void;
  /** Quick-range buttons. Defaults to `[7, 14, 30]`. */
  quickRanges?: number[];
  /** Whether to show the All-time button on the right. Defaults to `true`. */
  allowAllTime?: boolean;
  /** Whether to show the Custom range popover button. Defaults to `true`.
   *  Pages that haven't wired range selection through (e.g. EbTimeDetail)
   *  should pass false so users aren't shown a control that silently
   *  no-ops or falls back to "all time". */
  allowRange?: boolean;
  /** Extra classes for the outer pill container. */
  className?: string;
}

export function DaysRangePicker({
  value,
  onChange,
  quickRanges = [7, 14, 30],
  allowAllTime = true,
  allowRange = true,
  className,
}: Props) {
  // Stays as a string so the user can type freely without the input fighting them.
  const [customDays, setCustomDays] = useState<string>("");
  const [pickerOpen, setPickerOpen] = useState(false);

  // For the calendar popover. Seed from current selection if a range is active
  // so reopening the picker keeps the user's prior choice on screen.
  const initialRange: DateRange | undefined =
    value.kind === "range"
      ? {
          from: fromIsoDate(value.start) ?? undefined,
          to:   fromIsoDate(value.end)   ?? undefined,
        }
      : undefined;
  const [pendingRange, setPendingRange] = useState<DateRange | undefined>(initialRange);

  function applyCustomDays() {
    const trimmed = customDays.trim();
    if (trimmed === "") return;
    const n = Number(trimmed);
    if (!Number.isInteger(n) || n <= 0) return;
    onChange({ kind: "days", days: n });
  }

  function selectQuickRange(n: number) {
    setCustomDays("");
    onChange({ kind: "days", days: n });
  }

  function selectAllTime() {
    setCustomDays("");
    onChange({ kind: "all" });
  }

  function applyRange() {
    if (!pendingRange?.from || !pendingRange.to) return;
    onChange({
      kind: "range",
      start: toIsoDate(pendingRange.from),
      end:   toIsoDate(pendingRange.to),
    });
    setCustomDays("");
    setPickerOpen(false);
  }

  const activeDays = value.kind === "days" ? value.days : undefined;
  const rangeActive = value.kind === "range";

  return (
    <div
      className={cn(
        "inline-flex items-center gap-1 rounded-lg border border-gray-200 bg-white p-1",
        className,
      )}
    >
      {quickRanges.map((n) => (
        <button
          key={n}
          onClick={() => selectQuickRange(n)}
          className={cn(
            "h-8 px-3 rounded-md text-sm font-medium transition-colors",
            activeDays === n
              ? "bg-gray-900 text-white"
              : "text-gray-600 hover:bg-gray-100",
          )}
        >
          {n}d
        </button>
      ))}

      <div className="h-5 w-px bg-gray-200 mx-1" />

      <Input
        type="number"
        min={1}
        max={3650}
        value={customDays}
        onChange={(e) => setCustomDays(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") applyCustomDays();
        }}
        placeholder="N days"
        className="w-20 h-8 border-0 focus-visible:ring-0 px-2 text-sm"
      />
      <button
        onClick={applyCustomDays}
        className="h-8 px-2 rounded-md text-sm text-brand-600 hover:bg-brand-50"
      >
        Apply
      </button>

      {allowRange && <div className="h-5 w-px bg-gray-200 mx-1" />}

      {allowRange && (
      <Popover open={pickerOpen} onOpenChange={setPickerOpen}>
        <PopoverTrigger asChild>
          <button
            className={cn(
              "h-8 px-3 rounded-md text-sm font-medium inline-flex items-center gap-1.5 transition-colors",
              rangeActive
                ? "bg-gray-900 text-white"
                : "text-gray-600 hover:bg-gray-100",
            )}
          >
            <CalendarRange className="h-3.5 w-3.5" />
            {rangeActive ? rangeButtonLabel(value) : "Custom range"}
          </button>
        </PopoverTrigger>
        <PopoverContent align="end" className="p-0">
          <Calendar
            mode="range"
            numberOfMonths={2}
            captionLayout="dropdown-buttons"
            fromYear={2020}
            toYear={new Date().getFullYear() + 1}
            selected={pendingRange}
            onSelect={setPendingRange}
          />
          <div className="flex items-center justify-between border-t border-gray-100 px-3 py-2 text-sm">
            <span className="text-xs text-gray-500">
              {pendingRange?.from && pendingRange.to
                ? `${toIsoDate(pendingRange.from)} → ${toIsoDate(pendingRange.to)}`
                : "Pick a start and end date"}
            </span>
            <div className="flex items-center gap-2">
              <button
                onClick={() => {
                  setPendingRange(undefined);
                }}
                className="h-7 px-2 rounded-md text-xs text-gray-600 hover:bg-gray-100"
              >
                Clear
              </button>
              <button
                onClick={applyRange}
                disabled={!pendingRange?.from || !pendingRange.to}
                className={cn(
                  "h-7 px-3 rounded-md text-xs font-medium",
                  pendingRange?.from && pendingRange.to
                    ? "bg-brand-600 text-white hover:bg-brand-700"
                    : "bg-gray-100 text-gray-400 cursor-not-allowed",
                )}
              >
                Apply
              </button>
            </div>
          </div>
        </PopoverContent>
      </Popover>
      )}

      {allowAllTime && (
        <>
          <div className="h-5 w-px bg-gray-200 mx-1" />
          <button
            onClick={selectAllTime}
            className={cn(
              "h-8 px-3 rounded-md text-sm font-medium transition-colors",
              value.kind === "all"
                ? "bg-gray-900 text-white"
                : "text-gray-600 hover:bg-gray-100",
            )}
          >
            All time
          </button>
        </>
      )}
    </div>
  );
}

/** Compact label shown inside the range button when a range is active.
 *  e.g. "May 1 – May 14" (or "May 1, 2025 – Jan 3, 2026" across years). */
function rangeButtonLabel(v: Extract<DateSelection, { kind: "range" }>): string {
  const s = fromIsoDate(v.start);
  const e = fromIsoDate(v.end);
  if (!s || !e) return `${v.start} – ${v.end}`;
  const fmt = (d: Date) =>
    d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const sameYear = s.getFullYear() === e.getFullYear();
  return sameYear
    ? `${fmt(s)} – ${fmt(e)}`
    : `${fmt(s)} ${s.getFullYear()} – ${fmt(e)} ${e.getFullYear()}`;
}
