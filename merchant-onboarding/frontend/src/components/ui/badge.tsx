import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium whitespace-nowrap",
  {
    variants: {
      variant: {
        default: "bg-gray-100 text-gray-800",
        success: "bg-green-100 text-green-800",
        warning: "bg-amber-100 text-amber-800",
        danger:  "bg-red-100 text-red-800",
        info:    "bg-brand-100 text-brand-800",
        outline: "border border-gray-300 text-gray-700",
      },
    },
    defaultVariants: { variant: "default" },
  }
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export function statusVariant(status?: string | null): VariantProps<typeof badgeVariants>["variant"] {
  const s = (status || "").trim().toLowerCase();
  if (!s) return "outline";
  if (s.includes("live") || s === "complete") return "success";
  if (s.includes("pending") || s.includes("wip") || s.includes("kick")) return "warning";
  if (s.includes("rejected") || s.includes("failed") || s.includes("delay")) return "danger";
  return "info";
}
