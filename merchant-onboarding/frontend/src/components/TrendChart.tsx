import { useMemo } from "react";
import { format, parseISO } from "date-fns";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { TimeseriesPoint } from "@/lib/api";

interface Props {
  data: TimeseriesPoint[] | undefined;
  loading: boolean;
}

/** Stacked area chart: total kickoffs per day, with the approved subset
 *  highlighted underneath. Designed to be the visual centerpiece of the
 *  Dashboard hero — clean, big, with no chart-junk. */
export function TrendChart({ data, loading }: Props) {
  const series = useMemo(
    () =>
      (data || []).map((d) => ({
        date: d.date,
        approved: d.approved,
        pending: Math.max(0, d.count - d.approved),
        count: d.count,
      })),
    [data],
  );

  const total = series.reduce((s, d) => s + d.count, 0);
  const approved = series.reduce((s, d) => s + d.approved, 0);
  const peakDay = series.reduce((best, d) => (d.count > (best?.count ?? -1) ? d : best), series[0]);

  if (loading) {
    return (
      <div className="rounded-xl border border-gray-200 bg-white p-6">
        <div className="h-64 flex items-center justify-center text-sm text-gray-500">
          Loading trend…
        </div>
      </div>
    );
  }

  return (
    <article className="rounded-xl border border-gray-200 bg-white p-6">
      <header className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-base font-semibold text-gray-900">Kickoffs over time</h2>
          <p className="text-sm text-gray-500 mt-0.5">
            New merchants kicked off each day · approved subset shaded
          </p>
        </div>
        <div className="flex gap-6 text-right">
          <div>
            <p className="text-[10px] uppercase tracking-wider text-gray-500">Total</p>
            <p className="text-xl font-semibold text-gray-900 tabular-nums">{total}</p>
          </div>
          <div>
            <p className="text-[10px] uppercase tracking-wider text-gray-500">Approved</p>
            <p className="text-xl font-semibold text-emerald-600 tabular-nums">{approved}</p>
          </div>
          {/* Only show the peak card when there's actual data to peak at,
              otherwise we render "Peak day: 0" for the first (empty) day in
              the window which is just noise. */}
          {peakDay && total > 0 && peakDay.count > 0 && (
            <div>
              <p className="text-[10px] uppercase tracking-wider text-gray-500">Peak day</p>
              <p className="text-xl font-semibold text-gray-900 tabular-nums">
                {peakDay.count}
              </p>
              <p className="text-[10px] text-gray-500 tabular-nums">
                {format(parseISO(peakDay.date), "d MMM")}
              </p>
            </div>
          )}
        </div>
      </header>

      <div className="mt-4 -mx-2" style={{ height: 240 }}>
        {series.length === 0 || total === 0 ? (
          <div className="h-full flex items-center justify-center text-sm text-gray-400">
            No kickoffs in this window.
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={series} margin={{ top: 10, right: 16, bottom: 0, left: 4 }}>
              <defs>
                <linearGradient id="totalFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%"  stopColor="#6172f3" stopOpacity={0.35} />
                  <stop offset="100%" stopColor="#6172f3" stopOpacity={0}    />
                </linearGradient>
                <linearGradient id="approvedFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%"  stopColor="#10b981" stopOpacity={0.5}  />
                  <stop offset="100%" stopColor="#10b981" stopOpacity={0.1}  />
                </linearGradient>
              </defs>
              <CartesianGrid stroke="#f3f4f6" strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="date"
                stroke="#9ca3af"
                fontSize={11}
                tickLine={false}
                axisLine={false}
                tickFormatter={(d: string) => format(parseISO(d), "d MMM")}
                interval="preserveStartEnd"
                minTickGap={28}
              />
              <YAxis
                stroke="#9ca3af"
                fontSize={11}
                tickLine={false}
                axisLine={false}
                width={44}
                allowDecimals={false}
              />
              <Tooltip
                contentStyle={{
                  borderRadius: 8,
                  border: "1px solid #e5e7eb",
                  fontSize: 12,
                  boxShadow: "0 2px 8px rgba(0,0,0,0.04)",
                }}
                labelFormatter={(d: string) => format(parseISO(d), "EEE d MMM yyyy")}
                formatter={(v: number, name: string) => {
                  const label = name === "count" ? "Total" : name === "approved" ? "Approved" : name;
                  return [v, label];
                }}
              />
              <Area
                type="monotone"
                dataKey="count"
                stroke="#444ce7"
                strokeWidth={2}
                fill="url(#totalFill)"
                animationDuration={400}
              />
              <Area
                type="monotone"
                dataKey="approved"
                stroke="#10b981"
                strokeWidth={2}
                fill="url(#approvedFill)"
                animationDuration={400}
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>

      <div className="mt-2 flex items-center gap-4 text-xs text-gray-500">
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-3 rounded-sm bg-brand-600/70" />
          New kickoffs
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-3 rounded-sm bg-emerald-500/70" />
          Approved (Yes / Live)
        </span>
      </div>
    </article>
  );
}
