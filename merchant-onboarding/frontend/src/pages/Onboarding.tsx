import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarRange, ChevronLeft, ChevronRight, Download, Search, X } from "lucide-react";
import type { DateRange } from "react-day-picker";
import {
  Bar,
  BarChart,
  Cell,
  ResponsiveContainer,
  Tooltip as ReTooltip,
  XAxis,
  YAxis,
} from "recharts";

import { api, type EasebuzzPatch, type EasebuzzRow, type Stats, type SpeedMetric } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Badge, statusVariant } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Calendar } from "@/components/ui/calendar";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { EditableDateCell } from "@/components/EditableDateCell";
import { useToast } from "@/components/ui/toast";
import { toIsoDate } from "@/lib/date-selection";

// Canonical onboarding-status values after the one-time normalization.
const STATUS_OPTIONS = ["", "Yes", "No", "Live"];
const PAGE_SIZE_OPTIONS = [25, 50, 100, 200];

/** Parse an `<input type="number">` string into a non-negative integer.
 *  Rejects empty / NaN / fractional / negative — returns undefined so the
 *  filter clause stays inactive. Mirrors DaysRangePicker's input validation. */
function toNum(s: string): number | undefined {
  const trimmed = s.trim();
  if (!trimmed) return undefined;
  const n = Number(trimmed);
  if (!Number.isInteger(n) || n < 0) return undefined;
  return n;
}

/** Compact min/max numeric filter — two narrow inputs side by side. */
function MinMaxFilter({
  label, minVal, maxVal, onMin, onMax,
}: {
  label:  string;
  minVal: string;
  maxVal: string;
  onMin:  (v: string) => void;
  onMax:  (v: string) => void;
}) {
  const active = minVal !== "" || maxVal !== "";
  return (
    <div
      className={cn(
        "inline-flex items-center gap-1 h-8 rounded-md border px-2 transition-colors",
        active ? "border-brand-300 bg-brand-50" : "border-gray-200 bg-white",
      )}
    >
      <span className={cn(
        "text-xs font-medium pr-1",
        active ? "text-brand-900" : "text-gray-500",
      )}>{label}</span>
      <input
        type="number"
        min={0}
        value={minVal}
        onChange={(e) => onMin(e.target.value)}
        placeholder="min"
        aria-label={`${label} minimum`}
        className="w-12 h-6 bg-transparent text-sm tabular-nums text-right outline-none"
      />
      <span className="text-gray-400 text-xs">–</span>
      <input
        type="number"
        min={0}
        value={maxVal}
        onChange={(e) => onMax(e.target.value)}
        placeholder="max"
        aria-label={`${label} maximum`}
        className="w-12 h-6 bg-transparent text-sm tabular-nums text-right outline-none"
      />
    </div>
  );
}

/** One-decimal day count, "—" when null/undefined/NaN. */
function fmtDay(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${Math.round(n * 10) / 10}d`;
}

/** Single named percentile / stat row inside a MetricCard. */
function StatRow({ label, value, accent }: {
  label: string; value: string; accent?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between text-sm">
      <span className={cn("text-gray-500", accent && "text-gray-700 font-medium")}>{label}</span>
      <span className={cn(
        "font-mono tabular-nums",
        accent ? "text-gray-900 font-semibold" : "text-gray-700",
      )}>{value}</span>
    </div>
  );
}

// ── Color palette for the bucket histograms — mirrors the day-count
//    "speed dots" used elsewhere in the dashboard so 0–1d reads the same
//    everywhere (emerald → red as it slows down).
const BUCKET_COLORS: Record<string, string> = {
  "0-1d":   "#10b981",
  "2-3d":   "#84cc16",
  "4-7d":   "#f59e0b",
  "8-14d":  "#f97316",
  "15+d":   "#ef4444",
  unknown:  "#d1d5db",
};

function BucketTooltip({ active, payload, totalNumeric }: any) {
  if (!active || !payload?.length) return null;
  const item = payload[0].payload;
  const pct = totalNumeric > 0
    ? Math.round((item.count / totalNumeric) * 1000) / 10
    : 0;
  return (
    <div className="rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-xs shadow-sm">
      <div className="font-medium text-gray-900">{item.bucket}</div>
      <div className="text-gray-600">
        {item.count.toLocaleString()} merchants · {pct}%
      </div>
    </div>
  );
}

/** Per-metric analytics card — distribution histogram on top, summary
 *  stats grid below. Built to scan: median is the biggest number, with
 *  min/p25/p75/max/p90 secondary, and the bucket bars give a visual
 *  read of skew without needing the user to do mental math. */
function MetricCard({
  title,
  subtitle,
  metric,
}: {
  title: string;
  subtitle: string;
  metric: SpeedMetric;
}) {
  // Drop the "unknown" bucket from the chart — those rows have no parseable
  // duration and we don't want them dragging the y-axis around. Keep them
  // in the small text caption below the chart.
  const chartBuckets = metric.buckets.filter((b) => b.bucket !== "unknown");
  const unknownBucket = metric.buckets.find((b) => b.bucket === "unknown");
  const hasData = metric.total > 0;

  return (
    <article className="rounded-lg border border-gray-200 bg-white p-4 flex flex-col gap-3">
      <header>
        <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
        <p className="text-xs text-gray-500">{subtitle}</p>
      </header>

      <div className="h-28">
        {hasData ? (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={chartBuckets} margin={{ top: 4, right: 4, bottom: 0, left: 4 }}>
              <XAxis
                dataKey="bucket"
                stroke="#9ca3af"
                fontSize={10}
                tickLine={false}
                axisLine={false}
                interval={0}
              />
              <YAxis hide />
              <ReTooltip
                content={<BucketTooltip totalNumeric={metric.total} />}
                cursor={{ fill: "#f9fafb" }}
              />
              <Bar dataKey="count" radius={[3, 3, 0, 0]}>
                {chartBuckets.map((b) => (
                  <Cell key={b.bucket} fill={BUCKET_COLORS[b.bucket] ?? "#9ca3af"} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <div className="h-full flex items-center justify-center text-xs text-gray-400">
            No numbers to show for this filter
          </div>
        )}
      </div>

      {/* Headline median (plain English: the "typical" wait) + sample size. */}
      <div className="flex items-baseline justify-between">
        <div>
          <p className="text-[10px] uppercase tracking-wider text-gray-500">Typical wait</p>
          <p className="text-2xl font-semibold text-gray-900 font-mono tabular-nums">
            {fmtDay(metric.median)}
          </p>
        </div>
        <div className="text-right">
          <p className="text-[10px] uppercase tracking-wider text-gray-500">Measured</p>
          <p className="text-sm font-medium text-gray-700">
            {metric.total.toLocaleString()}
            {unknownBucket && unknownBucket.count > 0 && (
              <span className="text-gray-400"> · {unknownBucket.count} blank</span>
            )}
          </p>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-x-4 gap-y-1 pt-2 border-t border-gray-100">
        <StatRow label="Fastest"          value={fmtDay(metric.min)} />
        <StatRow label="Slowest"          value={fmtDay(metric.max)} />
        <StatRow label="Average"          value={fmtDay(metric.mean)} />
        <StatRow label="Quickest 1 in 4"  value={fmtDay(metric.p25)} />
        <StatRow label="3 in 4 within"    value={fmtDay(metric.p75)} />
        <StatRow label="9 in 10 within"   value={fmtDay(metric.p90)} />
      </div>
    </article>
  );
}

/** Horizontal segmented bar — one band per status, width proportional
 *  to count, with the count + percentage rendered alongside. */
function StatusBreakdownBar({
  byStatus,
  total,
}: {
  byStatus: Stats["by_status"];
  total: number;
}) {
  // Canonical buckets + colors. Anything else (typos, blank) lumps into "Other".
  const canon: Record<string, { color: string; order: number }> = {
    "Yes":     { color: "#10b981", order: 0 },
    "Live":    { color: "#3b82f6", order: 1 },
    "No":      { color: "#ef4444", order: 2 },
    "(blank)": { color: "#d1d5db", order: 3 },
  };
  const merged: Record<string, number> = {};
  for (const row of byStatus) {
    const key = canon[row.status] ? row.status : "Other";
    merged[key] = (merged[key] ?? 0) + row.count;
  }
  const otherColor = "#a1a1aa";
  const segments = Object.entries(merged)
    .map(([status, count]) => ({
      status,
      count,
      color: canon[status]?.color ?? otherColor,
      order: canon[status]?.order ?? 4,
    }))
    .sort((a, b) => a.order - b.order);

  return (
    <div className="space-y-2">
      {/* The segmented bar itself. */}
      <div className="flex h-7 w-full rounded-md overflow-hidden ring-1 ring-gray-200">
        {segments.map((seg) => (
          <div
            key={seg.status}
            title={`${seg.status}: ${seg.count}`}
            className="h-full transition-all"
            style={{
              width: total > 0 ? `${(seg.count / total) * 100}%` : "0%",
              background: seg.color,
            }}
          />
        ))}
        {total === 0 && (
          <div className="h-full w-full bg-gray-100" />
        )}
      </div>
      {/* Legend underneath, with counts + percentages. */}
      <ul className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
        {segments.map((seg) => {
          const pct = total > 0 ? Math.round((seg.count / total) * 1000) / 10 : 0;
          return (
            <li key={seg.status} className="flex items-center justify-between gap-2">
              <span className="flex items-center gap-2 min-w-0">
                <span
                  className="h-2.5 w-2.5 rounded-sm shrink-0"
                  style={{ background: seg.color }}
                />
                <span className="truncate text-gray-700">{seg.status}</span>
              </span>
              <span className="text-gray-600 font-mono tabular-nums shrink-0">
                {seg.count.toLocaleString()} · {pct}%
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** Rich analytics panel for the filtered subset.
 *
 *  Layout: header → 2-column top row (Sample + Approval/status breakdown)
 *  → 3-column metric row (EB days · Docs→S&K · K→S&K). Each metric card
 *  has its own bucket histogram + percentile grid. The user can scan
 *  every distributional property of the filtered slice without leaving
 *  the page.
 */
function FilteredAnalyticsPanel({
  data,
  loading,
}: {
  data: Stats | undefined;
  loading: boolean;
}) {
  if (loading || !data) {
    return (
      <section className="rounded-xl border border-brand-200 bg-brand-50/30 p-5 text-sm text-brand-700/80">
        Crunching the numbers…
      </section>
    );
  }

  const { total, by_status, speed } = data;
  // Approved = Yes + Live (matches Dashboard semantics). The other
  // buckets (No, blank, typos) reduce the rate.
  const yesCount  = by_status.find((s) => s.status === "Yes")?.count  ?? 0;
  const liveCount = by_status.find((s) => s.status === "Live")?.count ?? 0;
  const approved  = yesCount + liveCount;
  const approvalPct = total > 0
    ? Math.round((approved / total) * 1000) / 10
    : 0;

  return (
    <section className="rounded-xl border border-brand-200 bg-white shadow-sm">
      <header className="px-5 py-3 border-b border-gray-100 flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-baseline gap-3">
          <h2 className="text-sm font-semibold text-gray-900">What you're seeing</h2>
          <p className="text-xs text-gray-500">
            These numbers update each time you change a filter above.
          </p>
        </div>
        <span className="text-xs font-medium text-brand-700">
          {total.toLocaleString()} merchant{total === 1 ? "" : "s"}
        </span>
      </header>

      <div className="p-5 grid grid-cols-1 lg:grid-cols-3 gap-5">
        {/* Snapshot tile — total + approval rate, big numbers for quick scan. */}
        <article className="rounded-lg border border-gray-200 bg-white p-4 flex flex-col gap-3">
          <header>
            <h3 className="text-sm font-semibold text-gray-900">At a glance</h3>
            <p className="text-xs text-gray-500">Quick numbers for this view</p>
          </header>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <p className="text-[10px] uppercase tracking-wider text-gray-500">Merchants</p>
              <p className="text-3xl font-semibold text-gray-900 font-mono tabular-nums">
                {total.toLocaleString()}
              </p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-wider text-gray-500">Got their keys</p>
              <p className="text-3xl font-semibold text-emerald-600 font-mono tabular-nums">
                {total > 0 ? `${approvalPct}%` : "—"}
              </p>
              <p className="text-xs text-gray-500 mt-0.5">
                {approved.toLocaleString()} of {total.toLocaleString()}
              </p>
            </div>
          </div>
        </article>

        {/* Status breakdown — segmented bar + legend, takes 2 columns. */}
        <article className="lg:col-span-2 rounded-lg border border-gray-200 bg-white p-4 flex flex-col gap-3">
          <header>
            <h3 className="text-sm font-semibold text-gray-900">Where they're at</h3>
            <p className="text-xs text-gray-500">Status mix for the merchants in this view</p>
          </header>
          <StatusBreakdownBar byStatus={by_status} total={total} />
        </article>

        {/* Three metric cards: distribution + percentiles per day-count. */}
        <MetricCard
          title="Time Easebuzz took"
          subtitle="Days from us emailing Easebuzz to keys coming back — weekends don't count"
          metric={speed.time_taken_by_eb}
        />
        <MetricCard
          title="Docs to keys"
          subtitle="Days from receiving the docs until the keys were issued"
          metric={speed.salt_key_from_docs_recd}
        />
        <MetricCard
          title="Wait before Easebuzz"
          subtitle="Days from kickoff to keys, minus the time Easebuzz was actually working"
          metric={speed.salt_key_from_kickstart}
        />
      </div>
    </section>
  );
}

/** Compact duration cell: monospace number with a colored speed dot.
 *  Buckets match the Dashboard cards so the same gut-feel reads everywhere:
 *    0–1d emerald · 2–3d lime · 4–7d amber · 8–14d orange · 15+d red */
function DayCell({ v }: { v: string | null }) {
  if (!v || !v.trim()) return <span className="text-gray-300">—</span>;
  // Use Number() not parseInt() so "30abc" → NaN instead of 30 (silent garbage).
  const n = Number(v.trim());
  if (!Number.isFinite(n)) return <span className="font-mono text-gray-400">{v}</span>;
  const days = Math.abs(n);
  const dot =
    days <= 1   ? "bg-emerald-500"
    : days <= 3 ? "bg-lime-500"
    : days <= 7 ? "bg-amber-400"
    : days <= 14? "bg-orange-500"
                : "bg-red-500";
  return (
    <span className="inline-flex items-center gap-1.5 font-mono text-sm">
      <span className={cn("h-1.5 w-1.5 rounded-full shrink-0", dot)} />
      {days}
    </span>
  );
}

// ─────────────────────────────────────────────────────────────────────
// One row of the table. Extracted + memoized so a keystroke in the search
// box (which lives in the parent) doesn't re-render all 200 rows.
//
// Inputs are CONTROLLED via local state so they reflect the server's truth
// after a refetch and roll back automatically on a PATCH failure. The
// `key={r.id + r.last_synced_at}` on the parent map ensures local state is
// discarded any time the server's value changes (refetch arrives).
// ─────────────────────────────────────────────────────────────────────
interface RowProps {
  row: EasebuzzRow;
  onPatch: (id: string, body: EasebuzzPatch, optimisticLabel: string) => void;
}

const OnboardingRow = memo(function OnboardingRow({ row: r, onPatch }: RowProps) {
  // Status is read-only — the poller derives it from
  // (kickstart_date, salt_key_receipt) for seeded rows and respects
  // whatever the sheet says for the rest. See _normalize_seeded_status
  // in backend/app/poller/poll.py.
  const status = r.onboarding_status ?? "";
  const [opsRemarks, setOpsRemarks] = useState(r.ops_remarks ?? "");
  // Tracks whether the input currently has focus. We only sync local
  // state from the server when the user is NOT typing — otherwise an
  // in-flight PATCH's refetch could overwrite their keystrokes mid-type.
  // See audit fix (2026-05-26): "PATCH+remount race".
  const opsRemarksFocused = useRef(false);

  // Sync local state to the server value, but only when the input isn't
  // focused. Without this, the parent's key={`${id}:${last_synced_at}`}
  // remount used to silently discard the user's later keystrokes.
  useEffect(() => {
    if (!opsRemarksFocused.current) {
      setOpsRemarks(r.ops_remarks ?? "");
    }
  }, [r.ops_remarks]);

  // Show the "Needs review" badge only on seeded rows that are still
  // incomplete (missing kickstart OR salt&key). Once both endpoints
  // arrive on a later cron, the badge clears automatically — that's the
  // signal Ops uses to know the system is still chasing data.
  const isSeededIncomplete =
    r.source === "seeded" &&
    (!r.kickstart_date?.trim() || !r.salt_key_receipt?.trim());

  function commitOpsRemarks() {
    opsRemarksFocused.current = false;
    const trimmedPrev = (r.ops_remarks ?? "").trim();
    const trimmedNext = opsRemarks.trim();
    if (trimmedNext === trimmedPrev) return;
    onPatch(r.id, { ops_remarks: opsRemarks }, "Ops remarks");
  }

  return (
    <TableRow className={cn(isSeededIncomplete && "bg-amber-50/40")}>
      <TableCell className="font-medium">
        <div className="flex flex-col">
          <span>{r.merchant_name}</span>
          {isSeededIncomplete && (
            <Badge variant="warning" className="mt-0.5 w-fit">Needs review</Badge>
          )}
        </div>
      </TableCell>
      <TableCell className="text-gray-600">{r.merchant_size || "—"}</TableCell>
      <TableCell>
        <Badge variant={statusVariant(status)}>{status || "—"}</Badge>
      </TableCell>
      <TableCell>{r.kickstart_date || "—"}</TableCell>
      <TableCell>{r.docs_received_date || "—"}</TableCell>
      <TableCell>{r.kyc_completed_by_ops || "—"}</TableCell>
      <TableCell>
        <EditableDateCell
          value={r.date_email_sent_to_eb}
          ariaLabel="Edit Date of Email sent to EB"
          onSave={(v) => onPatch(r.id, { date_email_sent_to_eb: v }, "Email-to-EB date")}
        />
      </TableCell>
      <TableCell>
        <EditableDateCell
          value={r.salt_key_receipt}
          ariaLabel="Edit Salt & Key receipt date"
          onSave={(v) => onPatch(r.id, { salt_key_receipt: v }, "Salt & Key date")}
        />
      </TableCell>
      <TableCell className="text-right"><DayCell v={r.time_taken_by_eb} /></TableCell>
      <TableCell className="text-right"><DayCell v={r.salt_key_from_docs_recd} /></TableCell>
      <TableCell className="text-right"><DayCell v={r.salt_key_from_kickstart} /></TableCell>
      <TableCell className="max-w-xs">
        <input
          value={opsRemarks}
          onFocus={() => { opsRemarksFocused.current = true; }}
          onChange={(e) => setOpsRemarks(e.target.value)}
          onBlur={commitOpsRemarks}
          placeholder="—"
          aria-label="Edit ops remarks"
          className="w-full bg-transparent text-sm px-1 py-0.5 rounded hover:bg-gray-100 focus:bg-white focus:ring-1 focus:ring-brand-400 outline-none"
        />
      </TableCell>
    </TableRow>
  );
});

export default function OnboardingPage() {
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [delayed, setDelayed] = useState(false);
  const [pageSize, setPageSize] = useState(50);
  const [page, setPage] = useState(0);

  // Per-column filters — kept as strings so the inputs don't fight the
  // user. `toNum` converts to a finite non-negative integer or undefined.
  const [ebMin,      setEbMin]      = useState("");
  const [ebMax,      setEbMax]      = useState("");
  const [docsSkMin,  setDocsSkMin]  = useState("");
  const [docsSkMax,  setDocsSkMax]  = useState("");
  const [ksSkMin,    setKsSkMin]    = useState("");
  const [ksSkMax,    setKsSkMax]    = useState("");

  // Kickstart date-range picker. Stored as a DateRange so we can hand it
  // straight to react-day-picker; serialized to YYYY-MM-DD only when we
  // talk to the API.
  const [kickstartRange, setKickstartRange] = useState<DateRange | undefined>(undefined);
  const [kickstartPickerOpen, setKickstartPickerOpen] = useState(false);
  const [pendingRange, setPendingRange] = useState<DateRange | undefined>(undefined);

  // Salt & Key date-range filter — mirrors the Kickstart picker pattern.
  const [saltKeyRange, setSaltKeyRange] = useState<DateRange | undefined>(undefined);
  const [saltKeyPickerOpen, setSaltKeyPickerOpen] = useState(false);
  const [saltKeyPendingRange, setSaltKeyPendingRange] = useState<DateRange | undefined>(undefined);

  // CSV-download in-flight latch — prevents double-click spawning two
  // parallel downloads while the backend is still building the file.
  const [downloading, setDownloading] = useState(false);

  const rawFilterParams = useMemo(() => ({
    q: q || undefined,
    status: status || undefined,
    delayed: delayed || undefined,
    eb_days_min: toNum(ebMin),
    eb_days_max: toNum(ebMax),
    docs_sk_min: toNum(docsSkMin),
    docs_sk_max: toNum(docsSkMax),
    ks_sk_min:   toNum(ksSkMin),
    ks_sk_max:   toNum(ksSkMax),
    start_date: kickstartRange?.from ? toIsoDate(kickstartRange.from) : undefined,
    end_date:   kickstartRange?.to   ? toIsoDate(kickstartRange.to)   : undefined,
    salt_key_start: saltKeyRange?.from ? toIsoDate(saltKeyRange.from) : undefined,
    salt_key_end:   saltKeyRange?.to   ? toIsoDate(saltKeyRange.to)   : undefined,
  }), [q, status, delayed, ebMin, ebMax, docsSkMin, docsSkMax,
       ksSkMin, ksSkMax, kickstartRange, saltKeyRange]);

  // Debounce the filter params so a 300 ms pause is needed before each
  // numeric-input keystroke re-fires queries. Without this, typing "30"
  // in min/max fires 2 queries; typing "12345" fires 5. The backend
  // recomputes percentiles on every fire so this matters at scale.
  // Audit fix 2026-05-26.
  const [filterParams, setFilterParams] = useState(rawFilterParams);
  useEffect(() => {
    const t = setTimeout(() => setFilterParams(rawFilterParams), 300);
    return () => clearTimeout(t);
  }, [rawFilterParams]);

  const anyFilterActive = useMemo(
    () => Object.values(filterParams).some((v) => v !== undefined),
    [filterParams],
  );

  // Reset to first page whenever any filter changes.
  useEffect(() => { setPage(0); }, [filterParams, pageSize]);

  const list = useQuery({
    queryKey: ["easebuzz", { ...filterParams, page, pageSize }],
    queryFn: () =>
      api.easebuzz.list({
        ...filterParams,
        limit: pageSize,
        offset: page * pageSize,
      }),
    placeholderData: (prev) => prev,
  });

  // Page-wide stats card: always reflects the full database (no filters),
  // so users keep a stable reference point for totals/approval mix.
  const stats = useQuery({
    queryKey: ["easebuzz", "stats", "global"],
    queryFn: () => api.easebuzz.stats(),
  });

  // Filtered-subset analytics: reflects the active filter set + includes
  // seeded rows so the count matches the table the user is looking at.
  const filteredStats = useQuery({
    queryKey: ["easebuzz", "stats", "filtered", filterParams],
    queryFn: () => api.easebuzz.stats({ ...filterParams, include_seeded: true }),
    enabled: anyFilterActive,
  });

  const { toast } = useToast();

  // PATCH with explicit success / error handling:
  //  - on success → invalidate BOTH ["easebuzz"] and ["eb-times"] (the analytics
  //    page sits under a different key root and would otherwise show stale data)
  //  - on error   → surface a toast AND invalidate ["easebuzz"] anyway, so the
  //    refetch rolls the controlled inputs back to the server's truth.
  const patch = useMutation({
    mutationFn: ({ id, body }: { id: string; body: EasebuzzPatch; label?: string }) =>
      api.easebuzz.patch(id, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["easebuzz"] });
      qc.invalidateQueries({ queryKey: ["eb-times"] });
    },
    onError: (err, variables) => {
      toast({
        title: `Couldn't save ${variables.label ?? "change"}`,
        description: (err as Error).message,
        variant: "error",
      });
      // Force the rows to refetch so the controlled inputs reset to the
      // server's truth — otherwise the user keeps seeing their unsaved edit.
      qc.invalidateQueries({ queryKey: ["easebuzz"] });
    },
  });

  // Stable callback so memoized OnboardingRow doesn't re-render unnecessarily.
  const handleRowPatch = useCallback(
    (id: string, body: EasebuzzPatch, label: string) => {
      patch.mutate({ id, body, label });
    },
    [patch],
  );

  const totalByStatus = useMemo(() => {
    const m: Record<string, number> = {};
    for (const s of stats.data?.by_status || []) m[s.status] = s.count;
    return m;
  }, [stats.data]);

  const rows  = list.data?.rows  ?? [];
  const total = list.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const start = total === 0 ? 0 : page * pageSize + 1;
  const end   = Math.min((page + 1) * pageSize, total);
  const seededOnPage = rows.filter((r) => r.source === "seeded").length;

  return (
    <div className="p-6 space-y-4">
      <header className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-semibold text-gray-900">Easebuzz Onboarding</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            {stats.data ? `${stats.data.total.toLocaleString()} merchants total` : "Loading…"} ·{" "}
            Yes <span className="font-mono">{totalByStatus["Yes"] ?? 0}</span> ·{" "}
            No <span className="font-mono">{totalByStatus["No"] ?? 0}</span> ·{" "}
            Live <span className="font-mono">{totalByStatus["Live"] ?? 0}</span>
            {seededOnPage > 0 && (
              <>
                {" "}·{" "}
                <span className="text-amber-700">
                  {seededOnPage} need review on this page
                </span>
              </>
            )}
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <div className="relative">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-gray-400" />
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Search merchant name…"
              className="pl-8 w-72"
            />
          </div>
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className="h-9 rounded-md border border-gray-300 bg-white px-2 text-sm"
          >
            {STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {s || "All statuses"}
              </option>
            ))}
          </select>
          <Button
            variant={delayed ? "default" : "outline"}
            size="sm"
            onClick={() => setDelayed((v) => !v)}
          >
            Delayed only
          </Button>
        </div>
      </header>

      {/* Per-column filter rail. Compact min/max pairs for the three
          day-count columns plus a date-range picker for Kickstart. */}
      <div className="flex items-center gap-2 flex-wrap rounded-lg border border-gray-200 bg-white px-3 py-2">
        <span className="text-xs font-semibold uppercase tracking-wider text-gray-500 mr-1">Filter</span>

        {/* Kickstart range */}
        <Popover open={kickstartPickerOpen} onOpenChange={(open) => {
          if (open) setPendingRange(kickstartRange);
          setKickstartPickerOpen(open);
        }}>
          <PopoverTrigger asChild>
            <button
              className={cn(
                "inline-flex items-center gap-1.5 h-8 px-2.5 rounded-md text-sm border transition-colors",
                kickstartRange?.from && kickstartRange.to
                  ? "border-brand-300 bg-brand-50 text-brand-900"
                  : "border-gray-200 bg-white text-gray-700 hover:bg-gray-50",
              )}
            >
              <CalendarRange className="h-3.5 w-3.5" />
              {kickstartRange?.from && kickstartRange.to
                ? `Kickstart: ${toIsoDate(kickstartRange.from)} → ${toIsoDate(kickstartRange.to)}`
                : "Kickstart: any"}
              {kickstartRange?.from && kickstartRange.to && (
                <span
                  role="button"
                  tabIndex={0}
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    setKickstartRange(undefined);
                  }}
                  className="ml-0.5 text-gray-500 hover:text-gray-900 cursor-pointer"
                  aria-label="Clear kickstart range"
                >
                  <X className="h-3 w-3" />
                </span>
              )}
            </button>
          </PopoverTrigger>
          <PopoverContent align="start" className="p-0">
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
                  onClick={() => setPendingRange(undefined)}
                  className="h-7 px-2 rounded-md text-xs text-gray-600 hover:bg-gray-100"
                >
                  Clear
                </button>
                <button
                  onClick={() => {
                    if (!pendingRange?.from || !pendingRange.to) return;
                    setKickstartRange(pendingRange);
                    setKickstartPickerOpen(false);
                  }}
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

        {/* Salt & Key date range — same UX as the Kickstart picker. */}
        <Popover open={saltKeyPickerOpen} onOpenChange={(open) => {
          if (open) setSaltKeyPendingRange(saltKeyRange);
          setSaltKeyPickerOpen(open);
        }}>
          <PopoverTrigger asChild>
            <button
              className={cn(
                "inline-flex items-center gap-1.5 h-8 px-2.5 rounded-md text-sm border transition-colors",
                saltKeyRange?.from && saltKeyRange.to
                  ? "border-brand-300 bg-brand-50 text-brand-900"
                  : "border-gray-200 bg-white text-gray-700 hover:bg-gray-50",
              )}
            >
              <CalendarRange className="h-3.5 w-3.5" />
              {saltKeyRange?.from && saltKeyRange.to
                ? `Salt&Key: ${toIsoDate(saltKeyRange.from)} → ${toIsoDate(saltKeyRange.to)}`
                : "Salt&Key: any"}
              {saltKeyRange?.from && saltKeyRange.to && (
                <span
                  role="button"
                  tabIndex={0}
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    setSaltKeyRange(undefined);
                  }}
                  className="ml-0.5 text-gray-500 hover:text-gray-900 cursor-pointer"
                  aria-label="Clear salt&key range"
                >
                  <X className="h-3 w-3" />
                </span>
              )}
            </button>
          </PopoverTrigger>
          <PopoverContent align="start" className="p-0">
            <Calendar
              mode="range"
              numberOfMonths={2}
              captionLayout="dropdown-buttons"
              fromYear={2020}
              toYear={new Date().getFullYear() + 1}
              selected={saltKeyPendingRange}
              onSelect={setSaltKeyPendingRange}
            />
            <div className="flex items-center justify-between border-t border-gray-100 px-3 py-2 text-sm">
              <span className="text-xs text-gray-500">
                {saltKeyPendingRange?.from && saltKeyPendingRange.to
                  ? `${toIsoDate(saltKeyPendingRange.from)} → ${toIsoDate(saltKeyPendingRange.to)}`
                  : "Pick a start and end date"}
              </span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setSaltKeyPendingRange(undefined)}
                  className="h-7 px-2 rounded-md text-xs text-gray-600 hover:bg-gray-100"
                >
                  Clear
                </button>
                <button
                  onClick={() => {
                    if (!saltKeyPendingRange?.from || !saltKeyPendingRange.to) return;
                    setSaltKeyRange(saltKeyPendingRange);
                    setSaltKeyPickerOpen(false);
                  }}
                  disabled={!saltKeyPendingRange?.from || !saltKeyPendingRange.to}
                  className={cn(
                    "h-7 px-3 rounded-md text-xs font-medium",
                    saltKeyPendingRange?.from && saltKeyPendingRange.to
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

        <MinMaxFilter label="EB days"    minVal={ebMin}     maxVal={ebMax}
                      onMin={setEbMin}   onMax={setEbMax} />
        <MinMaxFilter label="Docs→S&K"   minVal={docsSkMin} maxVal={docsSkMax}
                      onMin={setDocsSkMin} onMax={setDocsSkMax} />
        <MinMaxFilter label="K→S&K"      minVal={ksSkMin}   maxVal={ksSkMax}
                      onMin={setKsSkMin} onMax={setKsSkMax} />

        <div className="ml-auto flex items-center gap-2">
          {/* Download CSV of whatever the filter currently matches.
              Disabled while in-flight so the button can't be double-clicked
              into multiple parallel downloads. */}
          <button
            onClick={async () => {
              if (downloading) return;
              setDownloading(true);
              try {
                await api.easebuzz.exportCsv(filterParams);
              } catch (e) {
                toast({
                  title: "Couldn't download CSV",
                  description: (e as Error).message,
                  variant: "error",
                });
              } finally {
                setDownloading(false);
              }
            }}
            disabled={downloading}
            className={cn(
              "inline-flex items-center gap-1.5 h-8 px-3 rounded-md text-xs font-medium border transition-colors",
              downloading
                ? "border-gray-200 bg-gray-100 text-gray-400 cursor-wait"
                : "border-brand-300 bg-white text-brand-700 hover:bg-brand-50",
            )}
            title="Download a CSV of every row matching the current filters"
          >
            <Download className="h-3.5 w-3.5" />
            {downloading ? "Preparing…" : "Download CSV"}
          </button>

          {anyFilterActive && (
            <button
              onClick={() => {
                setQ(""); setStatus(""); setDelayed(false);
                setEbMin(""); setEbMax("");
                setDocsSkMin(""); setDocsSkMax("");
                setKsSkMin(""); setKsSkMax("");
                setKickstartRange(undefined);
                setSaltKeyRange(undefined);
              }}
              className="h-8 px-3 rounded-md text-xs font-medium text-gray-600 hover:bg-gray-100"
            >
              Clear all
            </button>
          )}
        </div>
      </div>

      {/* Filtered-subset analytics — only shown when at least one filter
          is active, so the normal full-table view stays uncluttered. */}
      {anyFilterActive && (
        <FilteredAnalyticsPanel data={filteredStats.data} loading={filteredStats.isLoading} />
      )}

      {list.isLoading && !list.data && <div className="text-sm text-gray-500">Loading…</div>}
      {list.error && (
        <div className="text-sm text-red-600">
          Failed to load: {(list.error as Error).message}
        </div>
      )}

      {list.data && (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Merchant</TableHead>
              <TableHead>Size</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Kickstart</TableHead>
              <TableHead>Docs Recd</TableHead>
              <TableHead>KYC by Ops</TableHead>
              <TableHead>Email to EB</TableHead>
              <TableHead>Salt &amp; Key</TableHead>
              <TableHead className="text-right" title="Time Taken by EB — days from email-to-EB until Salt &amp; Key">EB days</TableHead>
              <TableHead className="text-right" title="Salt key from Docs received — days from docs arriving until S&amp;K">Docs→S&amp;K</TableHead>
              <TableHead className="text-right" title="Salt key from Kickstart, minus EB days — gap not attributable to EB">K→S&amp;K</TableHead>
              <TableHead>Ops Remarks</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {/* Composite key forces the memoized row to remount whenever the
                server's `last_synced_at` changes, so stale local input state
                is discarded (the bug the previous defaultValue setup had). */}
            {rows.map((r) => (
              <OnboardingRow
                key={r.id}
                row={r}
                onPatch={handleRowPatch}
              />
            ))}
            {rows.length === 0 && (
              <TableRow>
                <TableCell colSpan={14} className="text-center text-gray-500 py-8">
                  No merchants match your filters.
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      )}

      {/* Pagination bar */}
      {list.data && (
        <div className="flex items-center justify-between flex-wrap gap-3 pt-2">
          <div className="text-sm text-gray-600">
            {total === 0
              ? "0 results"
              : <>Showing <span className="font-mono">{start.toLocaleString()}–{end.toLocaleString()}</span> of <span className="font-mono">{total.toLocaleString()}</span></>}
            {list.isFetching && <span className="ml-2 text-xs text-gray-400">refreshing…</span>}
          </div>
          <div className="flex items-center gap-2">
            <label className="text-xs text-gray-500">Rows per page</label>
            <select
              value={pageSize}
              onChange={(e) => setPageSize(Number(e.target.value))}
              className="h-8 rounded-md border border-gray-300 bg-white px-2 text-sm"
            >
              {PAGE_SIZE_OPTIONS.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPage((p) => Math.max(0, p - 1))}
              disabled={page === 0}
            >
              <ChevronLeft className="h-4 w-4" />
              Prev
            </Button>
            <span className="text-sm text-gray-700 font-mono">
              {page + 1} / {totalPages}
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
              disabled={page >= totalPages - 1}
            >
              Next
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
