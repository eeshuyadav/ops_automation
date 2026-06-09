import { useMemo } from "react";
import { Gauge } from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { SpeedBreakdown } from "@/lib/api";

interface Props {
  data: SpeedBreakdown | undefined;
  windowLabel: string;
  needsReviewCount: number;
}

const BENCHMARK_DAYS = 3;
const BENCHMARK_COLOR = "#10b981";   // emerald — meeting the goal
const SLOW_COLOR = "#f59e0b";        // amber — over the goal

function fmtDays(n: number | null): string {
  if (n === null) return "—";
  if (n === 0) return "Same day";
  if (Number.isInteger(n)) return `${n} ${n === 1 ? "day" : "days"}`;
  return `${n.toFixed(1)} days`;
}

interface Row {
  label: string;
  median: number;
  /** p90 - median (the "slow tail") — second segment in the stacked bar. */
  tail: number;
  /** Original p90, kept around for the tooltip. */
  p90: number | null;
  recorded: number;
  fastPct: number;
}

function buildRows(data: SpeedBreakdown): Row[] {
  return [
    {
      label: "Kickstart → S&K",
      ...stat(data.salt_key_from_kickstart),
    },
    {
      label: "Docs → S&K",
      ...stat(data.salt_key_from_docs_recd),
    },
    {
      label: "Email → S&K",
      ...stat(data.time_taken_by_eb),
    },
  ];
}

function stat(m: { median: number | null; p90: number | null; total: number; buckets: { bucket: string; count: number }[] }) {
  const med = m.median ?? 0;
  const p90 = m.p90;
  const tail = p90 !== null && p90 > med ? p90 - med : 0;
  const counts: Record<string, number> = {};
  for (const b of m.buckets) counts[b.bucket] = b.count;
  const fast = (counts["0-1d"] ?? 0) + (counts["2-3d"] ?? 0);
  const fastPct = m.total > 0 ? Math.round((fast / m.total) * 100) : 0;
  return { median: med, tail, p90, recorded: m.total, fastPct };
}

/** Recharts custom tooltip — only show the row the cursor is on. */
function ChartTooltip({ active, payload }: { active?: boolean; payload?: any[] }) {
  if (!active || !payload || payload.length === 0) return null;
  const row: Row = payload[0].payload;
  return (
    <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-md text-xs">
      <p className="font-medium text-gray-900">{row.label}</p>
      <ul className="mt-1.5 space-y-0.5">
        <li className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-sm" style={{ background: row.median <= BENCHMARK_DAYS ? BENCHMARK_COLOR : SLOW_COLOR }} />
          <span className="text-gray-600">Typical</span>
          <span className="ml-auto font-mono text-gray-900">{fmtDays(row.median)}</span>
        </li>
        <li className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-sm" style={{ background: row.median <= BENCHMARK_DAYS ? BENCHMARK_COLOR : SLOW_COLOR, opacity: 0.3 }} />
          <span className="text-gray-600">9 in 10 finish within</span>
          <span className="ml-auto font-mono text-gray-900">{fmtDays(row.p90)}</span>
        </li>
        <li className="flex items-center gap-2 pt-1 mt-1 border-t border-gray-100">
          <span className="text-gray-600">Finish within {BENCHMARK_DAYS} days</span>
          <span className="ml-auto font-mono text-gray-900">{row.fastPct}%</span>
        </li>
        <li className="flex items-center gap-2">
          <span className="text-gray-600">Merchants tracked</span>
          <span className="ml-auto font-mono text-gray-900">{row.recorded.toLocaleString()}</span>
        </li>
      </ul>
    </div>
  );
}

export function SpeedPanel({ data, windowLabel, needsReviewCount }: Props) {
  const rows = useMemo(() => data ? buildRows(data) : [], [data]);

  const headlineMedian = data?.salt_key_from_kickstart.median ?? null;
  const headlineRecorded = data?.salt_key_from_kickstart.total ?? 0;

  // Auto-scale x-axis: round up the largest p90 to the next nice number.
  const maxX = useMemo(() => {
    const vals = rows.map((r) => r.median + r.tail);
    const m = Math.max(...vals, BENCHMARK_DAYS + 1, 1);
    return Math.ceil(m * 1.1);
  }, [rows]);

  return (
    <article className="rounded-xl border border-gray-200 bg-white p-6">
      <header className="flex items-start justify-between flex-wrap gap-4">
        <div>
          <h2 className="text-base font-semibold text-gray-900 flex items-center gap-2">
            <Gauge className="h-4 w-4 text-brand-500" />
            How fast is onboarding?
          </h2>
          <p className="text-sm text-gray-500 mt-0.5">
            {windowLabel}
            {needsReviewCount > 0 && (
              <> · excludes {needsReviewCount.toLocaleString()} in “Needs review”</>
            )}
          </p>
        </div>
        <div className="text-right">
          <p className="text-[10px] uppercase tracking-wider text-gray-500">Typical end-to-end</p>
          <p className="text-3xl font-semibold text-gray-900 tabular-nums leading-tight">
            {fmtDays(headlineMedian)}
          </p>
          {headlineRecorded > 0 && (
            <p className="text-xs text-gray-500">
              {headlineRecorded.toLocaleString()} merchants
            </p>
          )}
        </div>
      </header>

      <div className="mt-6" style={{ height: 200 }}>
        {rows.length === 0 ? (
          <div className="h-full flex items-center justify-center text-sm text-gray-400">
            No speed data in this window.
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart
              data={rows}
              layout="vertical"
              barCategoryGap="35%"
              margin={{ top: 28, right: 60, left: 0, bottom: 4 }}
            >
              <CartesianGrid stroke="#f3f4f6" strokeDasharray="3 3" horizontal={false} />
              <XAxis
                type="number"
                domain={[0, maxX]}
                stroke="#9ca3af"
                fontSize={11}
                tickLine={false}
                axisLine={false}
                tickFormatter={(v: number) => (v === 0 ? "0" : `${v}d`)}
              />
              <YAxis
                type="category"
                dataKey="label"
                stroke="#374151"
                fontSize={12}
                width={130}
                tickLine={false}
                axisLine={false}
              />
              <Tooltip cursor={{ fill: "#f9fafb" }} content={<ChartTooltip />} />
              <ReferenceLine
                x={BENCHMARK_DAYS}
                stroke="#6b7280"
                strokeDasharray="4 4"
                label={{ value: `${BENCHMARK_DAYS}-day target`, position: "top", fontSize: 10, fill: "#6b7280" }}
              />
              {/* Solid bar: 0 → median */}
              <Bar dataKey="median" stackId="dur" radius={[4, 0, 0, 4]}>
                {rows.map((r, i) => (
                  <Cell
                    key={i}
                    fill={r.median <= BENCHMARK_DAYS ? BENCHMARK_COLOR : SLOW_COLOR}
                  />
                ))}
              </Bar>
              {/* Lighter bar: median → p90 */}
              <Bar dataKey="tail" stackId="dur" radius={[0, 4, 4, 0]}>
                {rows.map((r, i) => (
                  <Cell
                    key={i}
                    fill={r.median <= BENCHMARK_DAYS ? BENCHMARK_COLOR : SLOW_COLOR}
                    fillOpacity={0.25}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>

      <footer className="mt-4 pt-3 border-t border-gray-100 flex items-center justify-between flex-wrap gap-3 text-xs text-gray-500">
        <ul className="flex items-center gap-4">
          <li className="flex items-center gap-1.5">
            <span className="h-2.5 w-3 rounded-sm" style={{ background: BENCHMARK_COLOR }} />
            Typical wait
          </li>
          <li className="flex items-center gap-1.5">
            <span className="h-2.5 w-3 rounded-sm" style={{ background: BENCHMARK_COLOR, opacity: 0.25 }} />
            Slowest 1 in 10
          </li>
          <li className="flex items-center gap-1.5">
            <span className="h-3 w-px border-l border-dashed border-gray-500" />
            <span>{BENCHMARK_DAYS}-day target</span>
          </li>
        </ul>
        <span className="text-gray-400">Hover bars for details · lower is better</span>
      </footer>
    </article>
  );
}
