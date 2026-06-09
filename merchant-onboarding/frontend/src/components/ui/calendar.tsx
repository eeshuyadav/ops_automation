"use client";

import * as React from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { DayPicker } from "react-day-picker";

import { cn } from "@/lib/utils";

export type CalendarProps = React.ComponentProps<typeof DayPicker>;

/** shadcn-style calendar wrapping react-day-picker v8.
 *  Caption: Month + Year dropdowns (jump anywhere fast), flanked by chevron
 *  Prev/Next buttons (adjacent-month navigation). All other shadcn defaults. */
export function Calendar({
  className,
  classNames,
  showOutsideDays = true,
  ...props
}: CalendarProps) {
  return (
    <DayPicker
      showOutsideDays={showOutsideDays}
      className={cn("p-3", className)}
      classNames={{
        months: "flex flex-col sm:flex-row gap-4",
        month: "space-y-3",
        caption: "flex justify-center pt-1 pb-1 relative items-center",
        // We render the dropdowns; the default caption_label is redundant.
        caption_label: "hidden",
        // Container for Month + Year selects.
        caption_dropdowns: "flex items-center gap-1.5",
        dropdown: cn(
          "h-7 rounded-md border border-gray-200 bg-white px-2 text-sm text-gray-900",
          "hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-brand-500",
          "cursor-pointer transition-colors",
        ),
        dropdown_month: "relative",
        dropdown_year: "relative",
        dropdown_icon: "ml-1 h-3 w-3 text-gray-400",
        // The "Month:" / "Year:" screen-reader labels — hide visually but keep for a11y.
        vhidden: "sr-only",
        nav: "flex items-center gap-1",
        nav_button: cn(
          "inline-flex items-center justify-center h-7 w-7 rounded-md border border-gray-200 bg-white",
          "hover:bg-gray-100 text-gray-700 transition-colors",
          "disabled:opacity-30 disabled:cursor-not-allowed",
        ),
        nav_button_previous: "absolute left-1",
        nav_button_next: "absolute right-1",
        table: "w-full border-collapse mt-2",
        head_row: "flex",
        head_cell: "text-gray-500 rounded-md w-9 font-normal text-[0.75rem] uppercase tracking-wider",
        row: "flex w-full mt-1",
        cell: cn(
          "h-9 w-9 text-center text-sm p-0 relative",
          "[&:has([aria-selected])]:bg-brand-50 first:[&:has([aria-selected])]:rounded-l-md last:[&:has([aria-selected])]:rounded-r-md",
          "focus-within:relative focus-within:z-20",
        ),
        day: cn(
          "h-9 w-9 p-0 font-normal text-gray-800 rounded-md inline-flex items-center justify-center",
          "hover:bg-gray-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 transition-colors",
          "aria-selected:opacity-100",
        ),
        day_selected: cn(
          "bg-brand-600 text-white hover:bg-brand-700 hover:text-white",
          "focus:bg-brand-600 focus:text-white",
        ),
        day_today: "ring-1 ring-brand-300 ring-inset",
        day_outside: "text-gray-300 aria-selected:bg-brand-50/40 aria-selected:text-gray-400",
        day_disabled: "text-gray-300 opacity-50 cursor-not-allowed",
        day_hidden: "invisible",
        ...classNames,
      }}
      components={{
        IconLeft: () => <ChevronLeft className="h-4 w-4" />,
        IconRight: () => <ChevronRight className="h-4 w-4" />,
      }}
      {...props}
    />
  );
}
