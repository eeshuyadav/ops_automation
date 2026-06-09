import { useState } from "react";
import { CalendarDays } from "lucide-react";
import { format as formatDate } from "date-fns";

import { cn } from "@/lib/utils";
import { Calendar } from "@/components/ui/calendar";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";

const MONTHS_FULL = ["january","february","march","april","may","june","july","august","september","october","november","december"];

/** Build a Date and verify the components round-trip — `new Date(2026, 13, 50)`
 *  silently rolls over to a different month/year, so we have to compare back. */
function safeBuildDate(year: number, monthIdx: number, day: number): Date | null {
  if (!Number.isInteger(year) || !Number.isInteger(monthIdx) || !Number.isInteger(day)) return null;
  if (monthIdx < 0 || monthIdx > 11) return null;
  if (day < 1 || day > 31) return null;
  const d = new Date(year, monthIdx, day);
  if (
    d.getFullYear() !== year ||
    d.getMonth() !== monthIdx ||
    d.getDate() !== day
  ) {
    // Caught Feb 30, Apr 31, etc.
    return null;
  }
  return d;
}

/** Parse any sheet-style date string into a JS Date, or null if we can't.
 *  Handles: 2026-05-11, 11-May-26, 11-May-2026, 27-January-26,
 *           "Apr 15, 2024", 21-01-2023, 21/01/2023. */
function textToDate(s: string | null | undefined): Date | null {
  if (!s) return null;
  const raw = s.trim();
  if (!raw) return null;
  let m: RegExpMatchArray | null;

  // ISO: 2026-05-11 (strict — no trailing garbage; `$` not `\b`).
  m = raw.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (m) return safeBuildDate(+m[1], +m[2] - 1, +m[3]);

  // DD-Mon-YY / DD-Mon-YYYY
  m = raw.match(/^(\d{1,2})[-\s\/]([A-Za-z]+)[-\s\/](\d{2,4})$/);
  if (m) {
    const mi = MONTHS_FULL.findIndex((x) => x.startsWith(m![2].toLowerCase()));
    if (mi >= 0) {
      const y = m[3].length === 2 ? 2000 + +m[3] : +m[3];
      return safeBuildDate(y, mi, +m[1]);
    }
  }

  // Mon DD, YYYY
  m = raw.match(/^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$/);
  if (m) {
    const mi = MONTHS_FULL.findIndex((x) => x.startsWith(m![1].toLowerCase()));
    if (mi >= 0) return safeBuildDate(+m[3], mi, +m[2]);
  }

  // DD-MM-YYYY or DD/MM/YYYY (dayfirst)
  m = raw.match(/^(\d{1,2})[-\/](\d{1,2})[-\/](\d{4})$/);
  if (m) return safeBuildDate(+m[3], +m[2] - 1, +m[1]);

  return null;
}

/** Save format: "14-Dec-2022" (matches the dominant pattern in the source sheet). */
function dateToDisplay(d: Date): string {
  return formatDate(d, "d-MMM-yyyy");
}

interface Props {
  value: string | null;
  onSave: (newValue: string) => void;
  ariaLabel?: string;
}

/** Click-to-edit date cell using the shadcn Popover + Calendar pattern.
 *  Displayed and saved as `D-Mon-YYYY` (e.g. `14-Dec-2022`). */
export function EditableDateCell({ value, onSave, ariaLabel = "Edit date" }: Props) {
  const [open, setOpen] = useState(false);
  const initial = textToDate(value) ?? undefined;

  function handleSelect(d: Date | undefined) {
    if (!d) return;
    const next = dateToDisplay(d);
    const prev = (value || "").trim();
    if (next !== prev) onSave(next);
    setOpen(false);
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={ariaLabel}
          className={cn(
            "group inline-flex items-center gap-1.5 text-sm text-left px-1.5 py-0.5 rounded whitespace-nowrap",
            "hover:bg-gray-100 hover:ring-1 hover:ring-gray-200 transition-colors cursor-pointer",
            !value && "text-gray-400 italic",
          )}
        >
          <CalendarDays className="h-3.5 w-3.5 text-gray-400 group-hover:text-brand-600 transition-colors shrink-0" />
          <span className="whitespace-nowrap">{value || "—"}</span>
        </button>
      </PopoverTrigger>
      <PopoverContent className="p-0" align="start">
        <Calendar
          mode="single"
          selected={initial}
          defaultMonth={initial}
          onSelect={handleSelect}
          captionLayout="dropdown-buttons"
          fromYear={2020}
          toYear={new Date().getFullYear() + 2}
          initialFocus
        />
      </PopoverContent>
    </Popover>
  );
}
