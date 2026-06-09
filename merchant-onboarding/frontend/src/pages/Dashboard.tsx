import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  ArrowRight,
  CheckCircle2,
  Clock,
  Sparkles,
  TrendingUp,
} from "lucide-react";

import { api } from "@/lib/api";
import { cn, timeAgo } from "@/lib/utils";
import { fmtDays } from "@/lib/format";
import { DaysRangePicker } from "@/components/DaysRangePicker";
import {
  ALL_TIME,
  type DateSelection,
  selectionLabel,
  selectionToApiParams,
} from "@/lib/date-selection";
import { TrendChart } from "@/components/TrendChart";
import { SpeedPanel } from "@/components/SpeedPanel";
import { EbTimePanel } from "@/components/EbTimePanel";

// ─────────────────────────────────────────────────────────────────────
// Hero tile — all 4 are the same width, height, and visual weight.
// Each follows the same template: tiny icon-chip + eyebrow label + big
// number + 1-line context + 1 thin sub-detail. That uniformity is what
// makes the grid feel deliberate instead of "two big, two small".
// ─────────────────────────────────────────────────────────────────────
type Accent = "brand" | "emerald" | "amber" | "gray";

const ACCENT: Record<Accent, { chipBg: string; chipRing: string; iconCls: string; barFill: string }> = {
  brand:   { chipBg: "bg-brand-50",   chipRing: "ring-brand-200",   iconCls: "text-brand-600",   barFill: "bg-brand-500"   },
  emerald: { chipBg: "bg-emerald-50", chipRing: "ring-emerald-200", iconCls: "text-emerald-600", barFill: "bg-emerald-500" },
  amber:   { chipBg: "bg-amber-50",   chipRing: "ring-amber-200",   iconCls: "text-amber-600",   barFill: "bg-amber-500"   },
  gray:    { chipBg: "bg-gray-100",   chipRing: "ring-gray-200",    iconCls: "text-gray-500",    barFill: "bg-gray-400"    },
};

function HeroTile({
  label,
  value,
  sub,
  icon: Icon,
  accent,
  detail,
  to,
}: {
  label: string;
  value: string;
  sub: string;
  icon: typeof Sparkles;
  accent: Accent;
  detail?: React.ReactNode;
  to?: string;
}) {
  const a = ACCENT[accent];
  const body = (
    <article className={cn(
      "rounded-xl border border-gray-200 bg-white p-5 flex flex-col gap-3 h-full transition-shadow",
      to && "hover:shadow-sm cursor-pointer",
    )}>
      <header className="flex items-center justify-between">
        <span className={cn("inline-flex h-9 w-9 rounded-lg items-center justify-center ring-1", a.chipBg, a.chipRing)}>
          <Icon className={cn("h-4 w-4", a.iconCls)} />
        </span>
        {to && <ArrowRight className="h-4 w-4 text-gray-300" />}
      </header>
      <div>
        <p className="text-3xl font-semibold text-gray-900 tabular-nums leading-tight">{value}</p>
        <p className="text-xs uppercase tracking-wider text-gray-500 mt-1.5">{label}</p>
        <p className="text-sm text-gray-500 mt-1">{sub}</p>
      </div>
      {detail && (
        <div className="mt-auto pt-3 border-t border-gray-100">
          {detail}
        </div>
      )}
    </article>
  );
  return to ? <Link to={to}>{body}</Link> : body;
}

// ─────────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────────
export default function DashboardPage() {
  // Default window: last 30 days. Most ops queries are recent-week scoped,
  // and "All time" pulls every row in the warehouse on load. The user can
  // still switch to All time / a custom range via the picker.
  const [selection, setSelection] = useState<DateSelection>({ kind: "days", days: 30 });
  const filterParams = selectionToApiParams(selection);

  // Once a seeded row auto-flips to onboarding_status='Yes' (the poller
  // does this when both kickstart_date and salt_key_receipt are in), it's
  // a legitimately-approved merchant and should count toward the totals.
  // include_seeded=true keeps every row in the picture; the old default
  // (exclude seeded) was a workaround for when seeded rows had blank
  // status, which the auto-derive now fixes.
  const statsParams = { ...filterParams, include_seeded: true };
  const stats = useQuery({
    queryKey: ["easebuzz", "stats", statsParams],
    queryFn: () => api.easebuzz.stats(statsParams),
  });
  const recentEb = useQuery({
    queryKey: ["easebuzz", "recent", filterParams],
    queryFn: () => api.easebuzz.list({ ...filterParams, limit: 6 }),
  });
  // Trend / EB-times endpoints need concrete bounds; on "All time" we fall
  // back to the last 90 days so the chart's x-axis stays sensible.
  const trendParams =
    selection.kind === "all" ? { days: 90 } : filterParams;
  const trend = useQuery({
    queryKey: ["easebuzz", "timeseries", trendParams],
    queryFn: () => api.easebuzz.timeseries(trendParams),
  });
  // "Needs review" = seeded rows still missing kickstart OR salt&key.
  // Backend computes this predicate in SQL via /api/easebuzz/needs-review
  // (audit fix: the old client-side filter on a 200-row list silently
  // capped the count once the backlog grew past 200).
  const needsReview = useQuery({
    queryKey: ["easebuzz", "needs-review"],
    queryFn: () => api.easebuzz.needsReview(),
  });
  const SLA_DAYS = 2;
  const ebTimesParams =
    selection.kind === "all"
      ? { days: 90, sla_days: SLA_DAYS }
      : { ...filterParams, sla_days: SLA_DAYS };
  const ebTimes = useQuery({
    queryKey: ["easebuzz", "eb-times", ebTimesParams],
    queryFn: () => api.easebuzz.ebTimes(ebTimesParams),
  });

  const totalByStatus = (stats.data?.by_status || []).reduce<Record<string, number>>(
    (acc, s) => ({ ...acc, [s.status]: s.count }),
    {},
  );
  const yes   = totalByStatus["Yes"]     ?? 0;
  const no    = totalByStatus["No"]      ?? 0;
  const live  = totalByStatus["Live"]    ?? 0;
  const blank = totalByStatus["(blank)"] ?? 0;
  const total = stats.data?.total ?? 0;

  const windowLabel = selectionLabel(selection);
  // The trend chart + EB-time analytics endpoints don't support "all time" —
  // we surface the fallback window so the chart title makes sense.
  const trendWindowLabel =
    selection.kind === "all" ? "last 90 days" : windowLabel;

  return (
    <div className="bg-gray-50 min-h-full">
      {/* Header */}
      <div className="px-8 pt-8 pb-6 bg-white border-b border-gray-200">
        <div className="flex items-start justify-between flex-wrap gap-4">
          <div>
            <h1 className="text-2xl font-semibold text-gray-900">Onboarding</h1>
            <p className="text-sm text-gray-500 mt-1">
              Showing <span className="font-medium text-gray-700">{windowLabel}</span>
              {total > 0 && <> · {total.toLocaleString()} merchants</>}
            </p>
          </div>
          <DaysRangePicker value={selection} onChange={setSelection} />
        </div>
      </div>

      <div className="px-8 py-8 space-y-8">
        {/* Hero metrics — 4 equal-width tiles (lead with the headline numbers) */}
        <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <HeroTile
            label="Approval rate"
            value={total === 0 ? "—" : `${((yes / Math.max(total, 1)) * 100).toFixed(1)}%`}
            sub={`${yes.toLocaleString()} of ${total.toLocaleString()} approved`}
            icon={TrendingUp}
            accent="brand"
            to="/onboarding"
            detail={
              total > 0 ? (
                <>
                  <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-gray-100">
                    <div className="bg-emerald-500 h-full" style={{ width: `${((yes + live) / Math.max(total, 1)) * 100}%` }} />
                    <div className="bg-gray-300 h-full"     style={{ width: `${((no + blank) / Math.max(total, 1)) * 100}%` }} />
                  </div>
                  <div className="mt-2 flex items-center justify-between text-[11px] text-gray-500">
                    <span>Yes <span className="text-gray-900 font-medium tabular-nums">{yes.toLocaleString()}</span></span>
                    <span>No <span className="text-gray-900 font-medium tabular-nums">{no.toLocaleString()}</span></span>
                    <span>Live <span className="text-gray-900 font-medium tabular-nums">{live.toLocaleString()}</span></span>
                  </div>
                </>
              ) : null
            }
          />
          {(() => {
            // KPI: how long Easebuzz typically takes to issue keys after we
            // hand the merchant over. This is the most actionable wait for
            // Ops (their SLA queue) and matches the "Easebuzz Speed"
            // detail page one click away in the sidebar.
            const m = stats.data?.speed.time_taken_by_eb;
            const median = m?.median ?? null;
            const p90    = m?.p90    ?? null;
            const fastPctText = (() => {
              if (!m || m.total === 0) return null;
              const counts: Record<string, number> = {};
              for (const b of m.buckets) counts[b.bucket] = b.count;
              const fast = (counts["0-1d"] ?? 0) + (counts["2-3d"] ?? 0);
              return `${Math.round((fast / m.total) * 100)}%`;
            })();
            return (
              <HeroTile
                label="How long Easebuzz takes"
                value={fmtDays(median)}
                sub="Typical wait from when we hand the merchant over until keys come back"
                icon={Clock}
                accent="emerald"
                to="/eb-time"
                detail={
                  <ul className="space-y-1 text-xs text-gray-600">
                    {p90 !== null && (
                      <li>
                        Most merchants (9 in 10) get keys within{" "}
                        <span className="font-semibold text-gray-900 tabular-nums">
                          {fmtDays(p90)}
                        </span>
                      </li>
                    )}
                    {fastPctText !== null && (
                      <li>
                        <span className="font-semibold text-gray-900 tabular-nums">
                          {fastPctText}
                        </span>{" "}
                        get keys within 3 days
                      </li>
                    )}
                  </ul>
                }
              />
            );
          })()}
          <HeroTile
            label="Needs review"
            value={(needsReview.data?.total ?? 0).toString()}
            sub="Auto-seeded merchants"
            icon={Sparkles}
            accent="amber"
            to="/onboarding"
            detail={
              <p className="text-xs text-amber-700">
                Awaiting ops to fill in status &amp; remarks
              </p>
            }
          />
          <HeroTile
            label="Now live"
            value={live.toString()}
            sub="Currently live on Easebuzz"
            icon={CheckCircle2}
            accent="emerald"
            detail={
              <p className="text-xs text-gray-500">
                {total > 0 ? `${((live / total) * 100).toFixed(1)}% of total` : "—"}
              </p>
            }
          />
        </section>

        {/* Kickoffs trend — sits above SpeedPanel as the 2nd section */}
        <TrendChart data={trend.data} loading={trend.isLoading} />

        {/* Onboarding speed — horizontal bar chart, 3rd section */}
        <SpeedPanel
          data={stats.data?.speed}
          windowLabel={windowLabel}
          needsReviewCount={needsReview.data?.total ?? 0}
        />

        {/* EB processing time — how quickly Easebuzz issues keys */}
        <EbTimePanel
          data={ebTimes.data}
          loading={ebTimes.isLoading}
          slaDays={SLA_DAYS}
        />

        {/* Needs review queue */}
        {needsReview.data && needsReview.data.items.length > 0 && (
          <section className="rounded-xl border border-gray-200 bg-white">
            <header className="px-6 pt-5 pb-4 border-b border-gray-100 flex items-center justify-between">
              <div>
                <h2 className="text-base font-semibold text-gray-900 flex items-center gap-2">
                  <Sparkles className="h-4 w-4 text-amber-500" />
                  Needs review
                </h2>
                <p className="text-sm text-gray-500 mt-0.5">
                  Recently auto-seeded merchants awaiting ops classification
                </p>
              </div>
              <Link
                to="/onboarding"
                className="text-sm text-brand-600 hover:underline inline-flex items-center gap-1"
              >
                View all {needsReview.data.total} <ArrowRight className="h-3 w-3" />
              </Link>
            </header>
            <ul className="divide-y divide-gray-100">
              {needsReview.data.items.slice(0, 6).map((r) => (
                <li key={r.id} className="px-6 py-3 flex items-center gap-4 hover:bg-gray-50 transition-colors">
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-gray-900 truncate">{r.merchant_name}</p>
                    <p className="text-xs text-gray-500 mt-0.5">
                      {r.merchant_size || "Size: —"}
                      {r.kyc_completed_by_ops && <> · KYC {r.kyc_completed_by_ops}</>}
                      {r.kickstart_date && <> · Kickstart {r.kickstart_date}</>}
                    </p>
                  </div>
                  <span className="text-xs px-2 py-1 rounded-md bg-amber-50 text-amber-700 ring-1 ring-amber-200">
                    Review
                  </span>
                </li>
              ))}
            </ul>
          </section>
        )}

        {/* Recent activity */}
        <section className="rounded-xl border border-gray-200 bg-white">
          <header className="px-6 pt-5 pb-4 border-b border-gray-100 flex items-center justify-between">
            <h2 className="text-base font-semibold text-gray-900">Recent activity</h2>
            <Link
              to="/onboarding"
              className="text-sm text-brand-600 hover:underline inline-flex items-center gap-1"
            >
              Open table <ArrowRight className="h-3 w-3" />
            </Link>
          </header>
          <ul className="divide-y divide-gray-100">
            {recentEb.data?.rows.map((e) => (
              <li key={e.id} className="px-6 py-3 flex items-center gap-4 hover:bg-gray-50 transition-colors">
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-gray-900 truncate">{e.merchant_name}</p>
                  <p className="text-xs text-gray-500 mt-0.5">
                    Kickstart {e.kickstart_date || "—"} · S&amp;K {e.salt_key_receipt || "—"}
                  </p>
                </div>
                <span className={cn(
                  "text-xs px-2 py-1 rounded-md ring-1",
                  e.onboarding_status === "Live"
                    ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
                    : e.onboarding_status === "Yes"
                    ? "bg-brand-50 text-brand-700 ring-brand-200"
                    : "bg-gray-50 text-gray-600 ring-gray-200",
                )}>
                  {e.onboarding_status || "—"}
                </span>
                <span className="text-xs text-gray-400 font-mono w-16 text-right">
                  {timeAgo(e.last_synced_at)}
                </span>
              </li>
            ))}
            {recentEb.data && recentEb.data.rows.length === 0 && (
              <li className="px-6 py-6 text-center text-sm text-gray-500">
                No merchants in this window.
              </li>
            )}
          </ul>
        </section>
      </div>
    </div>
  );
}
