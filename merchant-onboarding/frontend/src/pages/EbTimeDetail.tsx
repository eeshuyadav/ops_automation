import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { format, parseISO } from "date-fns";
import {
  Activity,
  AlertCircle,
  ArrowLeft,
  ArrowUpDown,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock,
  Gauge,
  Sparkles,
  Timer,
  TrendingUp,
  Users,
} from "lucide-react";
import {
  Area,
  AreaChart,
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

import { api } from "@/lib/api";
import type { EbAnalyticsResponse, EbTimeItem } from "@/lib/api";
import { cn } from "@/lib/utils";
// Shared format helpers (de-duplicated from local helpers below). `fmtDays`
// here is the short form ("3" / "0.5"); `fmtDaysLabel` is the verbose form
// ("3 days") — both came from the same source-of-truth file.
import {
  fmtDaysShort as fmtDays,
  fmtDays as fmtDaysLabel,
  fmtDate,
  fmtWindow,
  pct,
} from "@/lib/format";
import { Input } from "@/components/ui/input";
import { DaysRangePicker } from "@/components/DaysRangePicker";
import type { DateSelection } from "@/lib/date-selection";

// ─────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────
const OUTLIER_PAGE_SIZE = 25;
const FAST_COLOR = "#10b981";   // emerald-500
const SLOW_COLOR = "#ef4444";   // red-500

// ─────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────
// Format helpers come from @/lib/format (imported at top).

/** Normalize a free-text `merchant_size` value into a canonical bucket so
 *  the "By merchant size" table doesn't fragment across sheet typos like
 *  "Ent" / "ENT", "Emerging - Custom" / "Emerging - custom" /
 *  "Emerging - Custim". Returns "" for blank / "(unspecified)" so callers
 *  can treat that as "no group". */
function canonicalSize(raw: string | null | undefined): string {
  if (!raw) return "";
  const t = String(raw).trim();
  if (!t || t === "(unspecified)") return "";
  const lower = t.toLowerCase();
  // Strip whitespace + non-alphanum so "Emerging - Custom" and
  // "emerging-custim" collapse to the same key. We keep only letters
  // for the fuzzy match because the only meaningful suffix is "custom"
  // (with typos), and "ent" / "sme" are unambiguous.
  const compact = lower.replace(/[^a-z]/g, "");
  if (compact === "ent" || compact === "enterprise") return "ENT";
  if (compact === "sme") return "SME";
  if (compact.startsWith("emerging")) {
    const tail = compact.slice("emerging".length);
    if (!tail) return "Emerging";
    // Anything that looks like "custom" / "custim" / "custm" is the
    // Emerging-Custom bucket.
    if (tail.startsWith("cust")) return "Emerging - Custom";
    return "Emerging"; // unknown emerging-* suffix → roll up to base
  }
  // Unknown but non-empty — preserve as-is with whitespace collapsed.
  return t.replace(/\s+/g, " ");
}

// ─────────────────────────────────────────────────────────────────────
// Hero tile — mirrors the Dashboard.tsx HeroTile template exactly
// ─────────────────────────────────────────────────────────────────────
type Accent = "brand" | "emerald" | "red" | "gray" | "amber";

const ACCENT: Record<Accent, { chipBg: string; chipRing: string; iconCls: string }> = {
  brand:   { chipBg: "bg-brand-50",   chipRing: "ring-brand-200",   iconCls: "text-brand-600"   },
  emerald: { chipBg: "bg-emerald-50", chipRing: "ring-emerald-200", iconCls: "text-emerald-600" },
  red:     { chipBg: "bg-red-50",     chipRing: "ring-red-200",     iconCls: "text-red-600"     },
  amber:   { chipBg: "bg-amber-50",   chipRing: "ring-amber-200",   iconCls: "text-amber-600"   },
  gray:    { chipBg: "bg-gray-100",   chipRing: "ring-gray-200",    iconCls: "text-gray-500"    },
};

function HeroTile({
  label,
  value,
  sub,
  icon: Icon,
  accent,
}: {
  label: string;
  value: string;
  sub: string;
  icon: typeof Sparkles;
  accent: Accent;
}) {
  const a = ACCENT[accent];
  return (
    <article className="rounded-xl border border-gray-200 bg-white p-5 flex flex-col gap-3 h-full">
      <header>
        <span className={cn("inline-flex h-9 w-9 rounded-lg items-center justify-center ring-1", a.chipBg, a.chipRing)}>
          <Icon className={cn("h-4 w-4", a.iconCls)} />
        </span>
      </header>
      <div>
        <p className="text-3xl font-semibold text-gray-900 tabular-nums leading-tight">{value}</p>
        <p className="text-xs uppercase tracking-wider text-gray-500 mt-1.5">{label}</p>
        <p className="text-sm text-gray-500 mt-1">{sub}</p>
      </div>
    </article>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Tiny stat — used in the statistical summary grid
// ─────────────────────────────────────────────────────────────────────
function MicroStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wider text-gray-500">{label}</p>
      <p className="text-xl font-semibold text-gray-900 tabular-nums leading-tight mt-1">{value}</p>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Daily trend tooltip
// ─────────────────────────────────────────────────────────────────────
function TrendTooltip({ active, payload, label }: any) {
  if (!active || !payload || payload.length === 0) return null;
  const fast = payload.find((p: any) => p.dataKey === "fast")?.value ?? 0;
  const slow = payload.find((p: any) => p.dataKey === "slow")?.value ?? 0;
  const total = fast + slow;
  let dateLabel = label;
  try { dateLabel = format(parseISO(label), "EEE d MMM yyyy"); } catch { /* keep raw */ }
  return (
    <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-md text-xs">
      <p className="font-medium text-gray-900">{dateLabel}</p>
      <ul className="mt-1.5 space-y-0.5">
        <li className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-sm" style={{ background: FAST_COLOR }} />
          <span className="text-gray-600">On time</span>
          <span className="ml-auto font-mono text-gray-900">{fast}</span>
        </li>
        <li className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-sm" style={{ background: SLOW_COLOR }} />
          <span className="text-gray-600">Late</span>
          <span className="ml-auto font-mono text-gray-900">{slow}</span>
        </li>
        <li className="flex items-center gap-2 pt-1 mt-1 border-t border-gray-100">
          <span className="text-gray-600">Total</span>
          <span className="ml-auto font-mono text-gray-900">{total}</span>
        </li>
      </ul>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Histogram tooltip
// ─────────────────────────────────────────────────────────────────────
function HistogramTooltip({ active, payload }: any) {
  if (!active || !payload || payload.length === 0) return null;
  const row = payload[0].payload;
  return (
    <div className="rounded-lg border border-gray-200 bg-white px-3 py-2 shadow-md text-xs">
      <p className="font-medium text-gray-900">{row.day_bucket}</p>
      <ul className="mt-1.5 space-y-0.5">
        <li className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-sm" style={{ background: row.is_fast ? FAST_COLOR : SLOW_COLOR }} />
          <span className="text-gray-600">{row.is_fast ? "On time" : "Late"}</span>
          <span className="ml-auto font-mono text-gray-900">{row.count}</span>
        </li>
      </ul>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────────
export default function EbTimeDetail() {
  // EbTimeDetail keeps the trailing "last N days" semantics. The Custom
  // range button on the picker is hidden here via allowRange={false} so
  // we can keep the page's mental model simple; the Onboarding page (and
  // the per-row CSV export) is where you'd pick an explicit range.
  const [days, setDays] = useState<number | undefined>(30);
  const selection: DateSelection =
    days === undefined ? { kind: "all" } : { kind: "days", days };
  function handleSelection(v: DateSelection) {
    if (v.kind === "days") setDays(v.days);
    else setDays(undefined);  // "all time" path; range button is hidden
  }
  const [slaDays, setSlaDays] = useState<number>(2);
  const [merchantSize, setMerchantSize] = useState<string | undefined>(undefined);

  // Outlier table state
  const [outlierSortDir, setOutlierSortDir] = useState<"desc" | "asc">("desc");
  const [outlierMinDays, setOutlierMinDays] = useState<number | undefined>(undefined);
  const [outlierMinDaysInput, setOutlierMinDaysInput] = useState<string>("");
  const [outlierPage, setOutlierPage] = useState<number>(0);

  // ── Query ───────────────────────────────────────────────────────────
  // If user picked "All time" (days = undefined), fall back to 90 days for the
  // analytics endpoint so percentiles & histograms still have a sensible sample.
  const apiDays = days ?? 90;
  const query = useQuery({
    queryKey: ["eb-times", "analytics", { days, slaDays, merchantSize }],
    queryFn: () => api.easebuzz.ebTimesAnalytics({
      days: apiDays, sla_days: slaDays, merchant_size: merchantSize,
    }),
  });

  const data: EbAnalyticsResponse | undefined = query.data;

  // ── Derived ─────────────────────────────────────────────────────────
  const allSizes = useMemo(() => {
    if (!data) return [] as string[];
    const set = new Set<string>();
    for (const row of data.by_size) {
      const canon = canonicalSize(row.size);
      if (canon) set.add(canon);
    }
    return Array.from(set).sort();
  }, [data]);

  // Coalesce the merchant_size column's dirty values ("Ent" / "ENT",
  // "Emerging - Custom" / "Emerging - custom" / "Emerging - Custim",
  // blank / null / "(unspecified)") into a small canonical set before
  // the "By merchant size" table renders. Numeric aggregates (count,
  // fast, slow) sum across the merged keys so the table no longer shows
  // duplicate rows that fragment the data.
  const sortedBySize = useMemo(() => {
    if (!data) return [];
    type Row = (typeof data.by_size)[number];
    const merged = new Map<string, Row>();
    for (const r of data.by_size) {
      const canon = canonicalSize(r.size) || "(unspecified)";
      const cur = merged.get(canon);
      if (!cur) {
        merged.set(canon, { ...r, size: canon });
        continue;
      }
      // Sum the additive fields. For median/mean we'd need raw values to
      // recompute correctly; keep the dominant bucket's stats (good enough
      // for a summary table — exact distributions are shown elsewhere).
      const prevCount = cur.count || 0;
      const addCount  = r.count   || 0;
      cur.count = prevCount + addCount;
      cur.fast  = (cur.fast || 0) + (r.fast || 0);
      cur.slow  = (cur.slow || 0) + (r.slow || 0);
      if (addCount > prevCount) {
        cur.median = r.median;
        cur.mean   = r.mean;
      }
    }
    return Array.from(merged.values()).sort((a, b) => b.count - a.count);
  }, [data]);

  const outliers = useMemo(() => {
    if (!data) return [] as EbTimeItem[];
    let rows = data.items.filter((m) => !m.is_fast);
    if (outlierMinDays !== undefined) {
      rows = rows.filter((m) => m.days >= outlierMinDays);
    }
    rows = rows.slice().sort((a, b) =>
      outlierSortDir === "desc" ? b.days - a.days : a.days - b.days,
    );
    return rows;
  }, [data, outlierMinDays, outlierSortDir]);

  const outlierPageCount = Math.max(1, Math.ceil(outliers.length / OUTLIER_PAGE_SIZE));
  const pageStart = outlierPage * OUTLIER_PAGE_SIZE;
  const pageEnd = Math.min(pageStart + OUTLIER_PAGE_SIZE, outliers.length);
  const pageRows = outliers.slice(pageStart, pageEnd);

  const fastest = useMemo(() => {
    if (!data) return [] as EbTimeItem[];
    return data.items
      .filter((m) => m.is_fast)
      .slice()
      // Stable tie-break: ascending by days, then merchant name. Without
      // the name tiebreak, ties at 0 days surfaced an arbitrary subset
      // that could shift from page to page (audit fix 2026-05-26).
      .sort((a, b) => a.days - b.days
                   || a.merchant_name.localeCompare(b.merchant_name))
      .slice(0, 10);
  }, [data]);

  // ── Picker helpers ──────────────────────────────────────────────────
  // Use Number() + Number.isInteger so "30abc" rejects instead of silently
  // becoming 30 (which parseInt would have done).
  function applyMinDays() {
    const raw = outlierMinDaysInput.trim();
    const n = raw === "" ? NaN : Number(raw);
    setOutlierMinDays(Number.isInteger(n) && n > 0 ? n : undefined);
    setOutlierPage(0);
  }

  const windowLabel =
    days === undefined
      ? "all time"
      : `last ${days} day${days === 1 ? "" : "s"}`;

  // SLA threshold ReferenceLine x value — sits between "{sla}d" and "{sla+1}d"
  const slaThresholdBucket = `${slaDays}d`;

  // ── Render ──────────────────────────────────────────────────────────
  return (
    <div className="bg-gray-50 min-h-full">
      {/* ─────────── Header ─────────── */}
      <div className="px-8 pt-8 pb-6 bg-white border-b border-gray-200">
        <div className="flex items-start justify-between flex-wrap gap-4">
          <div className="min-w-0">
            <Link
              to="/dashboard"
              className="inline-flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-900 transition-colors"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              Back to overview
            </Link>
            <h1 className="text-2xl font-semibold text-gray-900 mt-2 flex items-center gap-2">
              <Timer className="h-5 w-5 text-brand-500" />
              How quickly does Easebuzz issue keys?
            </h1>
            <p className="text-sm text-gray-500 mt-1">
              The days between us emailing Easebuzz and them sending back the Salt &amp; Key
              {data && (
                <>
                  {" "}· {fmtWindow(data.window_start, data.window_end)} ·{" "}
                  {data.total.toLocaleString()} merchants
                </>
              )}
            </p>
          </div>

          <div className="flex items-center gap-3 flex-wrap">
            {/* Merchant size filter */}
            {allSizes.length > 0 && (
              <div className="inline-flex items-center gap-2 rounded-lg border border-gray-200 bg-white px-2.5 h-10">
                <Users className="h-3.5 w-3.5 text-gray-400" />
                <select
                  value={merchantSize ?? ""}
                  onChange={(e) => setMerchantSize(e.target.value || undefined)}
                  className="bg-transparent text-sm text-gray-700 focus:outline-none pr-1"
                >
                  <option value="">All sizes</option>
                  {allSizes.map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              </div>
            )}

            {/* Target-days picker */}
            <div className="inline-flex items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 h-10">
              <Gauge className="h-3.5 w-3.5 text-gray-400" />
              <span className="text-xs uppercase tracking-wider text-gray-500">Target</span>
              <Input
                type="number"
                min={1}
                max={90}
                value={String(slaDays)}
                onChange={(e) => {
                  const raw = e.target.value.trim();
                  const n = raw === "" ? NaN : Number(raw);
                  if (Number.isInteger(n) && n > 0 && n <= 90) setSlaDays(n);
                }}
                className="w-12 h-7 border-0 focus-visible:ring-0 px-1 text-sm tabular-nums"
              />
              <span className="text-sm text-gray-600">days</span>
            </div>

            {/* Date-range pill (shared component — same as Dashboard) */}
            <DaysRangePicker value={selection} onChange={handleSelection} allowRange={false} />
          </div>
        </div>
      </div>

      {/* ─────────── Body ─────────── */}
      <div className="px-8 py-8 space-y-8">
        {query.isLoading ? (
          <LoadingState />
        ) : query.isError ? (
          <ErrorState message={(query.error as Error)?.message || "Failed to load analytics"} />
        ) : !data || data.total === 0 ? (
          <EmptyState windowLabel={windowLabel} />
        ) : (
          <>
            {/* Headline KPIs — labels in plain English, no statistical jargon. */}
            <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-4">
              <HeroTile
                label="Merchants processed"
                value={data.total.toLocaleString()}
                sub={windowLabel}
                icon={Users}
                accent="gray"
              />
              <HeroTile
                label={`Got keys within ${slaDays} days`}
                value={`${pct(data.fast, data.total)}%`}
                sub={`${data.fast.toLocaleString()} merchants on time`}
                icon={CheckCircle2}
                accent="emerald"
              />
              <HeroTile
                label={`Took longer than ${slaDays} days`}
                value={`${pct(data.slow, data.total)}%`}
                sub={`${data.slow.toLocaleString()} merchants delayed`}
                icon={AlertCircle}
                accent="red"
              />
              <HeroTile
                label="Typical wait"
                value={fmtDays(data.stats.median)}
                sub="Half of merchants finish faster than this"
                icon={Clock}
                accent="brand"
              />
              <HeroTile
                label="Slowest cases take"
                value={fmtDays(data.stats.p90)}
                sub="9 out of 10 finish by this point"
                icon={TrendingUp}
                accent="amber"
              />
            </section>

            {/* Daily trend — stacked BAR chart (was AreaChart). Bars give discrete
                daily counts so a "slow" segment of 4 stays visible even next to a
                "fast" peak of 76. Areas interpolated between days, hiding small slow
                values inside a smooth green curve. */}
            <article className="rounded-xl border border-gray-200 bg-white p-6">
              <header className="flex items-start justify-between flex-wrap gap-3">
                <div>
                  <h2 className="text-base font-semibold text-gray-900">How many merchants did EB process each day?</h2>
                  <p className="text-sm text-gray-500 mt-0.5">
                    Green bars = finished within {slaDays} days · red = took longer
                  </p>
                </div>
                <div className="flex items-center gap-4 text-xs text-gray-500">
                  <span className="inline-flex items-center gap-1.5">
                    <span className="h-2 w-3 rounded-sm" style={{ background: FAST_COLOR }} />
                    On time
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    <span className="h-2 w-3 rounded-sm" style={{ background: SLOW_COLOR }} />
                    Late
                  </span>
                </div>
              </header>
              <div className="mt-4 -mx-2" style={{ height: 280 }}>
                {data.timeseries.length === 0 ? (
                  <div className="h-full flex items-center justify-center text-sm text-gray-400">
                    No daily data in this window.
                  </div>
                ) : (
                  <ResponsiveContainer width="100%" height="100%">
                    <BarChart data={data.timeseries} margin={{ top: 10, right: 16, bottom: 0, left: 4 }}>
                      <CartesianGrid stroke="#f3f4f6" strokeDasharray="3 3" vertical={false} />
                      <XAxis
                        dataKey="date"
                        stroke="#9ca3af"
                        fontSize={11}
                        tickLine={false}
                        axisLine={false}
                        tickFormatter={(d: string) => {
                          try { return format(parseISO(d), "d MMM"); } catch { return d; }
                        }}
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
                        cursor={{ fill: "#f9fafb" }}
                        content={<TrendTooltip />}
                      />
                      <Bar
                        dataKey="fast"
                        stackId="vol"
                        fill={FAST_COLOR}
                        radius={[0, 0, 0, 0]}
                        animationDuration={400}
                      />
                      <Bar
                        dataKey="slow"
                        stackId="vol"
                        fill={SLOW_COLOR}
                        radius={[2, 2, 0, 0]}
                        animationDuration={400}
                      />
                    </BarChart>
                  </ResponsiveContainer>
                )}
              </div>
            </article>

            {/* Distribution + By size */}
            <section className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {/* Histogram */}
              <article className="rounded-xl border border-gray-200 bg-white p-6">
                <header>
                  <h2 className="text-base font-semibold text-gray-900">How long do merchants wait?</h2>
                  <p className="text-sm text-gray-500 mt-0.5">
                    Number of merchants that received their keys after N days · target is {slaDays} days
                  </p>
                </header>
                <div className="mt-4 -mx-2" style={{ height: 260 }}>
                  {data.distribution.length === 0 ? (
                    <div className="h-full flex items-center justify-center text-sm text-gray-400">
                      No distribution data.
                    </div>
                  ) : (
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={data.distribution} margin={{ top: 10, right: 16, bottom: 0, left: 4 }}>
                        <CartesianGrid stroke="#f3f4f6" strokeDasharray="3 3" vertical={false} />
                        <XAxis
                          dataKey="day_bucket"
                          stroke="#9ca3af"
                          fontSize={11}
                          tickLine={false}
                          axisLine={false}
                          interval={0}
                        />
                        <YAxis
                          stroke="#9ca3af"
                          fontSize={11}
                          tickLine={false}
                          axisLine={false}
                          width={44}
                          allowDecimals={false}
                        />
                        <Tooltip content={<HistogramTooltip />} cursor={{ fill: "#f9fafb" }} />
                        <ReferenceLine
                          x={slaThresholdBucket}
                          stroke="#6b7280"
                          strokeDasharray="4 4"
                          label={{ value: `${slaDays}-day target`, position: "top", fontSize: 10, fill: "#6b7280" }}
                          ifOverflow="extendDomain"
                        />
                        <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                          {data.distribution.map((b, i) => (
                            <Cell key={i} fill={b.is_fast ? FAST_COLOR : SLOW_COLOR} />
                          ))}
                        </Bar>
                      </BarChart>
                    </ResponsiveContainer>
                  )}
                </div>
                <footer className="mt-2 flex items-center gap-4 text-xs text-gray-500">
                  <span className="inline-flex items-center gap-1.5">
                    <span className="h-2 w-3 rounded-sm" style={{ background: FAST_COLOR }} />
                    On time
                  </span>
                  <span className="inline-flex items-center gap-1.5">
                    <span className="h-2 w-3 rounded-sm" style={{ background: SLOW_COLOR }} />
                    Late
                  </span>
                </footer>
              </article>

              {/* By merchant size */}
              <article className="rounded-xl border border-gray-200 bg-white p-6">
                <header>
                  <h2 className="text-base font-semibold text-gray-900">By merchant size</h2>
                  <p className="text-sm text-gray-500 mt-0.5">
                    Are any segments slower than others?
                  </p>
                </header>
                <div className="mt-4 rounded-lg border border-gray-200 overflow-hidden">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="bg-gray-50 text-[10px] uppercase tracking-wider text-gray-500">
                        <th className="text-left font-medium px-4 py-2.5">Size</th>
                        <th className="text-right font-medium px-4 py-2.5">Total</th>
                        <th className="text-right font-medium px-4 py-2.5">On time</th>
                        <th className="text-right font-medium px-4 py-2.5">Late</th>
                        <th className="text-right font-medium px-4 py-2.5">Typical wait</th>
                        <th className="text-right font-medium px-4 py-2.5">Average wait</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100">
                      {sortedBySize.length === 0 ? (
                        <tr>
                          <td colSpan={6} className="px-4 py-6 text-center text-sm text-gray-400">
                            No size data.
                          </td>
                        </tr>
                      ) : (
                        sortedBySize.map((row) => (
                          <tr key={row.size || "—"} className="hover:bg-gray-50 transition-colors">
                            <td className="px-4 py-3 align-top">
                              <p className="text-gray-900 font-medium">{row.size || "—"}</p>
                              <div className="mt-1.5 flex h-1.5 w-full overflow-hidden rounded-full bg-gray-100">
                                <div
                                  className="h-full bg-emerald-500"
                                  style={{ width: `${pct(row.fast, row.count)}%` }}
                                />
                                <div
                                  className="h-full bg-red-500"
                                  style={{ width: `${pct(row.slow, row.count)}%` }}
                                />
                              </div>
                            </td>
                            <td className="px-4 py-3 text-right text-gray-900 tabular-nums">
                              {row.count.toLocaleString()}
                            </td>
                            <td className="px-4 py-3 text-right text-emerald-700 tabular-nums">
                              {row.fast.toLocaleString()}
                              <span className="text-gray-400"> · {pct(row.fast, row.count)}%</span>
                            </td>
                            <td className="px-4 py-3 text-right text-red-700 tabular-nums">
                              {row.slow.toLocaleString()}
                              <span className="text-gray-400"> · {pct(row.slow, row.count)}%</span>
                            </td>
                            <td className="px-4 py-3 text-right text-gray-700 tabular-nums">
                              {fmtDays(row.median)}
                            </td>
                            <td className="px-4 py-3 text-right text-gray-700 tabular-nums">
                              {fmtDays(row.mean)}
                            </td>
                          </tr>
                        ))
                      )}
                    </tbody>
                  </table>
                </div>
              </article>
            </section>

            {/* "How it usually goes" — plain-English replacement for the old
                 9-stat statistical grid (Min/p25/Median/Mean/p75/p90/p99/Max/StdDev).
                 Same data, told as a story instead of a stats table. */}
            <article className="rounded-xl border border-gray-200 bg-white p-6">
              <header>
                <h2 className="text-base font-semibold text-gray-900 flex items-center gap-2">
                  <Activity className="h-4 w-4 text-brand-500" />
                  How it usually goes
                </h2>
                <p className="text-sm text-gray-500 mt-0.5">
                  Reading the data across {data.total.toLocaleString()} merchants in {windowLabel}
                </p>
              </header>

              {/* 4 milestone cards, friendly labels */}
              <div className="mt-6 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
                <article className="rounded-lg border border-emerald-200 bg-emerald-50/40 p-4">
                  <p className="text-[10px] uppercase tracking-wider text-emerald-700">Fastest merchant</p>
                  <p className="mt-1 text-2xl font-semibold text-gray-900 tabular-nums">
                    {fmtDays(data.stats.min)}
                  </p>
                  <p className="text-xs text-gray-600 mt-1">Best case in this window</p>
                </article>
                <article className="rounded-lg border border-gray-200 bg-white p-4">
                  <p className="text-[10px] uppercase tracking-wider text-gray-500">Half finish within</p>
                  <p className="mt-1 text-2xl font-semibold text-gray-900 tabular-nums">
                    {fmtDays(data.stats.median)}
                  </p>
                  <p className="text-xs text-gray-600 mt-1">The typical merchant's wait</p>
                </article>
                <article className="rounded-lg border border-gray-200 bg-white p-4">
                  <p className="text-[10px] uppercase tracking-wider text-gray-500">9 in 10 finish within</p>
                  <p className="mt-1 text-2xl font-semibold text-gray-900 tabular-nums">
                    {fmtDays(data.stats.p90)}
                  </p>
                  <p className="text-xs text-gray-600 mt-1">Only the slowest 10% take longer</p>
                </article>
                <article className="rounded-lg border border-red-200 bg-red-50/40 p-4">
                  <p className="text-[10px] uppercase tracking-wider text-red-700">Slowest merchant</p>
                  <p className="mt-1 text-2xl font-semibold text-gray-900 tabular-nums">
                    {fmtDays(data.stats.max)}
                  </p>
                  <p className="text-xs text-gray-600 mt-1">Worst case in this window</p>
                </article>
              </div>

              {/* One-line narrative */}
              <p className="mt-6 pt-4 border-t border-gray-100 text-sm text-gray-700 leading-relaxed">
                Across <span className="font-medium text-gray-900 tabular-nums">{data.total.toLocaleString()}</span> merchants,
                the typical wait is{" "}
                <span className="font-medium text-gray-900">{fmtDaysLabel(data.stats.median)}</span>.{" "}
                <span className="font-medium text-emerald-700 tabular-nums">{pct(data.fast, data.total)}%</span>{" "}
                received their keys within the {slaDays}-day target,
                {data.slow > 0 ? (
                  <>
                    {" "}while{" "}
                    <span className="font-medium text-red-700 tabular-nums">{data.slow.toLocaleString()}</span>{" "}
                    merchants ({pct(data.slow, data.total)}%) waited longer.
                  </>
                ) : (
                  <> with no delays in this window.</>
                )}
              </p>
            </article>

            {/* Late merchants (over the day target) */}
            <article className="rounded-xl border border-gray-200 bg-white p-6">
              <header className="flex items-start justify-between flex-wrap gap-3">
                <div>
                  <h2 className="text-base font-semibold text-gray-900 flex items-center gap-2">
                    <AlertCircle className="h-4 w-4 text-red-500" />
                    Merchants who waited longer than {slaDays} days
                  </h2>
                  <p className="text-sm text-gray-500 mt-0.5">
                    {outliers.length.toLocaleString()} merchant{outliers.length === 1 ? "" : "s"} late · sorted by wait time
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <div className="inline-flex items-center gap-2 rounded-lg border border-gray-200 bg-white px-2.5 h-9">
                    <span className="text-xs uppercase tracking-wider text-gray-500">Min days</span>
                    <Input
                      type="number"
                      min={1}
                      value={outlierMinDaysInput}
                      onChange={(e) => setOutlierMinDaysInput(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") applyMinDays(); }}
                      onBlur={applyMinDays}
                      placeholder="—"
                      className="w-16 h-7 border-0 focus-visible:ring-0 px-1 text-sm tabular-nums"
                    />
                  </div>
                  <button
                    onClick={() => setOutlierSortDir((d) => (d === "desc" ? "asc" : "desc"))}
                    className="inline-flex items-center gap-1.5 h-9 px-3 rounded-lg border border-gray-200 bg-white text-sm text-gray-700 hover:bg-gray-50 transition-colors"
                  >
                    <ArrowUpDown className="h-3.5 w-3.5 text-gray-400" />
                    Days {outlierSortDir === "desc" ? "↓" : "↑"}
                  </button>
                </div>
              </header>

              {outliers.length === 0 ? (
                <div className="mt-6 rounded-lg border border-emerald-200 bg-emerald-50/50 px-4 py-10 flex items-center justify-center gap-3">
                  <CheckCircle2 className="h-6 w-6 text-emerald-600" />
                  <p className="text-sm text-emerald-800 font-medium">
                    Great — every merchant received their keys on time!
                  </p>
                </div>
              ) : (
                <>
                  <div className="mt-6 rounded-lg border border-gray-200 overflow-hidden">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="bg-gray-50 text-[10px] uppercase tracking-wider text-gray-500">
                          <th className="text-left font-medium px-4 py-2.5 w-12">#</th>
                          <th className="text-left font-medium px-4 py-2.5">Merchant</th>
                          <th className="text-left font-medium px-4 py-2.5">Size</th>
                          <th className="text-left font-medium px-4 py-2.5">Email Sent</th>
                          <th className="text-left font-medium px-4 py-2.5">S&amp;K Issued</th>
                          <th className="text-left font-medium px-4 py-2.5">Kickstart</th>
                          <th className="text-right font-medium px-4 py-2.5">Days</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-100">
                        {pageRows.map((m, i) => (
                          <tr key={m.id} className="hover:bg-gray-50 transition-colors">
                            <td className="px-4 py-2.5 text-gray-400 tabular-nums">
                              {pageStart + i + 1}
                            </td>
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
                            <td className="px-4 py-2.5 text-gray-600 tabular-nums">
                              {fmtDate(m.kickstart_date)}
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

                  {/* Pagination */}
                  <footer className="mt-4 flex items-center justify-between text-xs text-gray-500">
                    <p className="tabular-nums">
                      Showing <span className="text-gray-900 font-medium">{pageStart + 1}</span>–
                      <span className="text-gray-900 font-medium">{pageEnd}</span> of{" "}
                      <span className="text-gray-900 font-medium">{outliers.length.toLocaleString()}</span>
                    </p>
                    <div className="inline-flex items-center gap-1">
                      <button
                        onClick={() => setOutlierPage((p) => Math.max(0, p - 1))}
                        disabled={outlierPage === 0}
                        className={cn(
                          "inline-flex items-center gap-1 h-8 px-2.5 rounded-md border border-gray-200 text-sm transition-colors",
                          outlierPage === 0
                            ? "text-gray-300 cursor-not-allowed"
                            : "text-gray-700 hover:bg-gray-50",
                        )}
                      >
                        <ChevronLeft className="h-3.5 w-3.5" />
                        Prev
                      </button>
                      <span className="px-2 tabular-nums text-gray-500">
                        Page {outlierPage + 1} / {outlierPageCount}
                      </span>
                      <button
                        onClick={() => setOutlierPage((p) => Math.min(outlierPageCount - 1, p + 1))}
                        disabled={outlierPage >= outlierPageCount - 1}
                        className={cn(
                          "inline-flex items-center gap-1 h-8 px-2.5 rounded-md border border-gray-200 text-sm transition-colors",
                          outlierPage >= outlierPageCount - 1
                            ? "text-gray-300 cursor-not-allowed"
                            : "text-gray-700 hover:bg-gray-50",
                        )}
                      >
                        Next
                        <ChevronRight className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </footer>
                </>
              )}
            </article>

            {/* Fastest performers */}
            <article className="rounded-xl border border-gray-200 bg-white p-6">
              <header>
                <h2 className="text-base font-semibold text-gray-900 flex items-center gap-2">
                  <CheckCircle2 className="h-4 w-4 text-emerald-500" />
                  Fastest merchants
                </h2>
                <p className="text-sm text-gray-500 mt-0.5">
                  The {fastest.length} merchants who got their keys quickest
                </p>
              </header>
              {fastest.length === 0 ? (
                <p className="mt-4 text-sm text-gray-400">No fast merchants in this window.</p>
              ) : (
                <ul className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-x-6 divide-y divide-gray-100 sm:divide-y-0">
                  {fastest.map((m, i) => (
                    <li
                      key={m.id}
                      className={cn(
                        "py-2.5 flex items-center justify-between gap-4",
                        i >= fastest.length / 2 ? "" : "",
                      )}
                    >
                      <div className="flex items-center gap-3 min-w-0">
                        <span className="inline-flex h-6 w-6 shrink-0 rounded-full bg-emerald-50 ring-1 ring-emerald-200 text-[11px] font-semibold text-emerald-700 items-center justify-center tabular-nums">
                          {i + 1}
                        </span>
                        <p className="text-sm font-medium text-gray-900 truncate">
                          {m.merchant_name}
                        </p>
                      </div>
                      <span className="text-sm text-emerald-700 font-medium tabular-nums shrink-0">
                        {fmtDaysLabel(m.days)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </article>
          </>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Loading / Error / Empty
// ─────────────────────────────────────────────────────────────────────
function LoadingState() {
  return (
    <>
      <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-6 gap-4">
        {[0, 1, 2, 3, 4, 5].map((i) => (
          <div key={i} className="h-32 rounded-xl border border-gray-200 bg-white animate-pulse" />
        ))}
      </section>
      <div className="h-72 rounded-xl border border-gray-200 bg-white animate-pulse" />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="h-72 rounded-xl border border-gray-200 bg-white animate-pulse" />
        <div className="h-72 rounded-xl border border-gray-200 bg-white animate-pulse" />
      </div>
    </>
  );
}

function ErrorState({ message }: { message: string }) {
  return (
    <article className="rounded-xl border border-red-200 bg-red-50/50 p-8 text-center">
      <AlertCircle className="h-8 w-8 text-red-500 mx-auto" />
      <p className="mt-3 text-sm font-medium text-red-900">Couldn't load analytics</p>
      <p className="mt-1 text-sm text-red-700">{message}</p>
    </article>
  );
}

function EmptyState({ windowLabel }: { windowLabel: string }) {
  return (
    <article className="rounded-xl border border-gray-200 bg-white p-12 flex flex-col items-center justify-center text-center">
      <Timer className="h-10 w-10 text-gray-300" />
      <p className="mt-4 text-sm font-medium text-gray-700">No EB processing time data</p>
      <p className="mt-1 text-sm text-gray-500">
        Nothing recorded for <span className="font-medium text-gray-700">{windowLabel}</span>.
        Try widening the window or removing filters.
      </p>
    </article>
  );
}
