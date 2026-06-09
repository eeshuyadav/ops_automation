import { useMemo } from "react";
import { format, parseISO } from "date-fns";
import { AlertCircle, CheckCircle2, Timer, Users } from "lucide-react";

import type { EbTimesResponse } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Props {
  data: EbTimesResponse | undefined;
  loading: boolean;
  slaDays: number;
}

// ─────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────
function fmtWindow(start: string, end: string): string {
  try {
    return `${format(parseISO(start), "d MMM")} – ${format(parseISO(end), "d MMM")}`;
  } catch {
    return `${start} – ${end}`;
  }
}

function pct(n: number, total: number): number {
  if (total <= 0) return 0;
  return Math.round((n / total) * 100);
}

function fmtDate(raw: string | null): string {
  if (!raw) return "—";
  // The sheet stores free-text dates; try ISO first, fall back to raw.
  try {
    const d = parseISO(raw);
    if (!Number.isNaN(d.getTime())) return format(d, "d MMM");
  } catch {
    /* not ISO — fine */
  }
  return raw;
}

// ─────────────────────────────────────────────────────────────────────────
// Stat tile — same template as Dashboard HeroTile but compact
// ─────────────────────────────────────────────────────────────────────────
type Accent = "gray" | "emerald" | "red";

const ACCENT: Record<Accent, { chipBg: string; chipRing: string; iconCls: string }> = {
  gray:    { chipBg: "bg-gray-100",   chipRing: "ring-gray-200",    iconCls: "text-gray-500"    },
  emerald: { chipBg: "bg-emerald-50", chipRing: "ring-emerald-200", iconCls: "text-emerald-600" },
  red:     { chipBg: "bg-red-50",     chipRing: "ring-red-200",     iconCls: "text-red-600"     },
};

function StatTile({
  label,
  value,
  sub,
  icon: Icon,
  accent,
}: {
  label: string;
  value: string;
  sub?: string;
  icon: typeof Users;
  accent: Accent;
}) {
  const a = ACCENT[accent];
  return (
    <div className="rounded-lg border border-gray-200 bg-white p-4 flex items-start gap-3">
      <span
        className={cn(
          "inline-flex h-9 w-9 shrink-0 rounded-lg items-center justify-center ring-1",
          a.chipBg,
          a.chipRing,
        )}
      >
        <Icon className={cn("h-4 w-4", a.iconCls)} />
      </span>
      <div className="min-w-0">
        <p className="text-[10px] uppercase tracking-wider text-gray-500">{label}</p>
        <p className="text-2xl font-semibold text-gray-900 tabular-nums leading-tight mt-0.5">
          {value}
        </p>
        {sub && <p className="text-xs text-gray-500 mt-1">{sub}</p>}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Loading skeleton
// ─────────────────────────────────────────────────────────────────────────
function Skeleton() {
  return (
    <div className="space-y-6">
      <div>
        <div className="flex items-center justify-between mb-2">
          <div className="h-3 w-48 bg-gray-100 animate-pulse rounded" />
          <div className="h-3 w-48 bg-gray-100 animate-pulse rounded" />
        </div>
        <div className="h-2 w-full bg-gray-100 animate-pulse rounded-full" />
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {[0, 1, 2].map((i) => (
          <div key={i} className="h-[88px] rounded-lg border border-gray-200 bg-gray-50 animate-pulse" />
        ))}
      </div>
      <div className="h-48 rounded-lg bg-gray-100 animate-pulse" />
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────
// Main component
// ─────────────────────────────────────────────────────────────────────────
export function EbTimePanel({ data, loading, slaDays }: Props) {
  const slowest = useMemo(() => {
    if (!data) return [];
    return data.items
      .filter((m) => !m.is_fast)
      .sort((a, b) => b.days - a.days)
      .slice(0, 10);
  }, [data]);

  return (
    <article className="rounded-xl border border-gray-200 bg-white p-6">
      <header className="flex items-start justify-between flex-wrap gap-4">
        <div>
          <h2 className="text-base font-semibold text-gray-900 flex items-center gap-2">
            <Timer className="h-4 w-4 text-brand-500" />
            How quickly does Easebuzz issue keys?
          </h2>
          <p className="text-sm text-gray-500 mt-0.5">
            Days between us emailing Easebuzz and them sending back the Salt &amp; Key
            {data && (
              <>
                {" "}
                · {fmtWindow(data.window_start, data.window_end)} · {data.total.toLocaleString()}{" "}
                merchants
              </>
            )}
          </p>
        </div>
      </header>

      {loading ? (
        <div className="mt-6">
          <Skeleton />
        </div>
      ) : !data || data.total === 0 ? (
        <div className="mt-6 flex flex-col items-center justify-center py-12 text-center">
          <Timer className="h-8 w-8 text-gray-300" />
          <p className="mt-3 text-sm text-gray-500">No EB processing time data in this window.</p>
        </div>
      ) : (
        <>
          {/* Stacked SLA bar */}
          <div className="mt-6">
            <div className="flex items-center justify-between text-xs">
              <span className="flex items-center gap-1.5 text-gray-700">
                <span className="h-2 w-2 rounded-full bg-emerald-500" />
                Within {slaDays} {slaDays === 1 ? "day" : "days"} ·{" "}
                <span className="font-medium text-gray-900 tabular-nums">
                  {data.fast.toLocaleString()}
                </span>{" "}
                merchants ({pct(data.fast, data.total)}%)
              </span>
              <span className="flex items-center gap-1.5 text-gray-700">
                Over {slaDays} {slaDays === 1 ? "day" : "days"} ·{" "}
                <span className="font-medium text-gray-900 tabular-nums">
                  {data.slow.toLocaleString()}
                </span>{" "}
                merchants ({pct(data.slow, data.total)}%)
                <span className="h-2 w-2 rounded-full bg-red-500" />
              </span>
            </div>
            <div className="mt-2 flex h-2 w-full overflow-hidden rounded-full bg-gray-100">
              <div
                className="h-full bg-emerald-500"
                style={{ width: `${pct(data.fast, data.total)}%` }}
              />
              <div
                className="h-full bg-red-500"
                style={{ width: `${pct(data.slow, data.total)}%` }}
              />
            </div>
          </div>

          {/* Stat tiles */}
          <section className="mt-6 grid grid-cols-1 sm:grid-cols-3 gap-3">
            <StatTile
              label="Total merchants"
              value={data.total.toLocaleString()}
              sub={`In ${fmtWindow(data.window_start, data.window_end)}`}
              icon={Users}
              accent="gray"
            />
            <StatTile
              label={`On time (≤ ${slaDays} ${slaDays === 1 ? "day" : "days"})`}
              value={data.fast.toLocaleString()}
              sub={`${pct(data.fast, data.total)}% of total`}
              icon={CheckCircle2}
              accent="emerald"
            />
            <StatTile
              label={`Late (> ${slaDays} ${slaDays === 1 ? "day" : "days"})`}
              value={data.slow.toLocaleString()}
              sub={`${pct(data.slow, data.total)}% of total`}
              icon={AlertCircle}
              accent="red"
            />
          </section>

          {/* Slowest merchants */}
          <section className="mt-6">
            <div className="flex items-baseline justify-between mb-3">
              <h3 className="text-sm font-semibold text-gray-900">Merchants who waited longest</h3>
              <p className="text-xs text-gray-500">
                Top {Math.min(10, data.slow)} merchants who took longer than {slaDays} days
              </p>
            </div>

            {slowest.length === 0 ? (
              <div className="rounded-lg border border-emerald-200 bg-emerald-50/50 px-4 py-6 flex items-center justify-center gap-3">
                <CheckCircle2 className="h-5 w-5 text-emerald-600" />
                <p className="text-sm text-emerald-800">
                  Every merchant got their keys on time — great work!
                </p>
              </div>
            ) : (
              <div className="rounded-lg border border-gray-200 overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-50 text-[10px] uppercase tracking-wider text-gray-500">
                      <th className="text-left font-medium px-4 py-2.5">Merchant</th>
                      <th className="text-left font-medium px-4 py-2.5">Size</th>
                      <th className="text-left font-medium px-4 py-2.5">Email Sent</th>
                      <th className="text-left font-medium px-4 py-2.5">S&amp;K Issued</th>
                      <th className="text-right font-medium px-4 py-2.5">Days</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100">
                    {slowest.map((m) => (
                      <tr key={m.id} className="hover:bg-gray-50 transition-colors">
                        <td className="px-4 py-2.5 text-gray-900 font-medium truncate max-w-[260px]">
                          {m.merchant_name}
                        </td>
                        <td className="px-4 py-2.5 text-gray-600">
                          {m.merchant_size || "—"}
                        </td>
                        <td className="px-4 py-2.5 text-gray-600 tabular-nums">
                          {fmtDate(m.email_date)}
                        </td>
                        <td className="px-4 py-2.5 text-gray-600 tabular-nums">
                          {fmtDate(m.sk_date)}
                        </td>
                        <td className="px-4 py-2.5 text-right">
                          <span className="inline-flex items-center gap-2 font-semibold text-gray-900 tabular-nums">
                            <span className="h-2 w-2 rounded-full bg-red-500" />
                            {m.days}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {data.slow > slowest.length && slowest.length > 0 && (
              <p className="mt-2 text-xs text-gray-400 text-right">
                Showing {slowest.length} of {data.slow.toLocaleString()} late merchants
              </p>
            )}
          </section>
        </>
      )}
    </article>
  );
}
