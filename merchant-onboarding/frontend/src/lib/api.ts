// Hits the FastAPI backend. Vite proxies /api → http://localhost:8001 in dev.

import { clearAuth, readToken } from "@/lib/auth";

const BASE = "";

// JWT bearer token, read fresh on every request from localStorage so a
// just-issued login takes effect immediately and a logout in another tab
// invalidates this tab on the next call too.
function buildHeaders(extra?: HeadersInit): HeadersInit {
  const headers: Record<string, string> = {
    "content-type": "application/json",
    ...(extra as Record<string, string> | undefined),
  };
  const token = readToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: buildHeaders(init?.headers),
  });
  if (!res.ok) {
    // 401 = token missing, invalid, or expired. Clear stored auth and
    // bounce to /login so the AuthContext picks up the empty state on
    // its next render. Skip the auto-bounce when the URL itself is the
    // login endpoint (the form needs to render the error inline).
    if (res.status === 401 && !path.startsWith("/api/auth/login")) {
      clearAuth();
      if (typeof window !== "undefined" && window.location.pathname !== "/login") {
        const next = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.assign(`/login?next=${next}`);
      }
    }
    let detail = "";
    try {
      const body = await res.json();
      detail = (body && (body.detail || body.message)) || "";
    } catch {
      try { detail = await res.text(); } catch { /* ignore */ }
    }
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// ---------------------------------------------------------------------------
// Types — mirror app/schemas.py
// ---------------------------------------------------------------------------
// Mirrors backend MerchantOut — cols A and C..K of the Gokwik Submerchant
// list (col B and everything past K are intentionally dropped).
export interface Merchant {
  id: string;
  mid: string;                              // A
  eb_go_live_date: string | null;           // C
  kyc_spoc: string | null;                  // D
  gokwik_kyc_complete_date: string | null;  // E
  merchant_name: string | null;             // F
  entity_name: string | null;               // G
  email: string | null;                     // H
  website: string | null;                   // I
  onboarding: string | null;                // J
  entity: string | null;                    // K
  first_seen_at: string;
  last_synced_at: string;
}

export interface EasebuzzRow {
  id: string;
  merchant_id: string | null;
  merchant_name: string;
  merchant_size: string | null;
  onboarding_status: string | null;
  kickstart_date: string | null;
  kickstart_time: string | null;
  docs_received_date: string | null;
  docs_received_time: string | null;
  days_taken_ks_to_ds: string | null;
  time_taken_ks_to_ds: string | null;
  kyc_completed_by_ops: string | null;
  days_taken_kyc: string | null;
  date_email_sent_to_eb: string | null;
  salt_key_receipt: string | null;
  time_taken_by_eb: string | null;
  salt_key_from_docs_recd: string | null;
  salt_key_from_kickstart: string | null;
  reasons_for_delay_in_eb: string | null;
  promise: string | null;
  delivery: string | null;
  remarks: string | null;
  delay_at_gk: string | null;
  delay_by_merchant: string | null;
  ops_remarks: string | null;
  source: string;
  last_edited_in_dashboard_at: string | null;
  last_synced_at: string;
}

export interface EasebuzzPatch {
  onboarding_status?: string | null;
  kickstart_date?: string | null;
  docs_received_date?: string | null;
  kyc_completed_by_ops?: string | null;
  date_email_sent_to_eb?: string | null;
  salt_key_receipt?: string | null;
  promise?: string | null;
  delivery?: string | null;
  remarks?: string | null;
  delay_at_gk?: string | null;
  delay_by_merchant?: string | null;
  ops_remarks?: string | null;
}

export type SpeedBucketKey = "0-1d" | "2-3d" | "4-7d" | "8-14d" | "15+d" | "unknown";

export interface SpeedBucket {
  bucket: SpeedBucketKey;
  count: number;
}

export interface SpeedMetric {
  total: number;
  median: number | null;
  p90: number | null;
  // Extended stats. Older deployments may not return these (the API
  // marks them optional and FastAPI omits unset fields), so the frontend
  // must guard with `??` / null-checks.
  min?:  number | null;
  max?:  number | null;
  mean?: number | null;
  p25?:  number | null;
  p75?:  number | null;
  buckets: SpeedBucket[];
}

export interface SpeedBreakdown {
  time_taken_by_eb: SpeedMetric;
  salt_key_from_docs_recd: SpeedMetric;
  salt_key_from_kickstart: SpeedMetric;
}

export interface Stats {
  total: number;
  by_status: { status: string; count: number }[];
  speed: SpeedBreakdown;
}

export interface TimeseriesPoint {
  date: string;     // YYYY-MM-DD
  count: number;    // total kickoffs that day
  approved: number; // subset that became Yes/Live
}

export interface EbTimeItem {
  id: string;
  merchant_name: string;
  merchant_size: string | null;
  days: number;
  is_fast: boolean;
  email_date: string | null;
  sk_date: string | null;
  kickstart_date: string | null;
}

export interface EbTimesResponse {
  sla_days: number;
  window_start: string;   // ISO date
  window_end:   string;   // ISO date
  total: number;
  fast:  number;
  slow:  number;
  items: EbTimeItem[];
}

export interface EbStats {
  mean:   number | null;
  median: number | null;
  p25:    number | null;
  p75:    number | null;
  p90:    number | null;
  p99:    number | null;
  min:    number | null;
  max:    number | null;
  stddev: number | null;
}

export interface EbTimeseriesPoint {
  date: string;
  fast: number;
  slow: number;
}

export interface EbBySizeRow {
  size:   string;
  count:  number;
  fast:   number;
  slow:   number;
  median: number | null;
  mean:   number | null;
}

export interface EbDistributionBucket {
  day_bucket: string;
  count:      number;
  is_fast:    boolean;
}

export interface EbAnalyticsResponse {
  window_start: string;
  window_end:   string;
  sla_days:     number;
  total: number;
  fast:  number;
  slow:  number;
  stats:        EbStats;
  timeseries:   EbTimeseriesPoint[];
  by_size:      EbBySizeRow[];
  distribution: EbDistributionBucket[];
  items:        EbTimeItem[];   // reuse the existing EbTimeItem interface
}

export interface EasebuzzList {
  total: number;        // count after filters, before limit/offset
  rows: EasebuzzRow[];  // the current page
}

// ---------------------------------------------------------------------------
// Endpoints
// ---------------------------------------------------------------------------
function qs(p: Record<string, string | number | boolean | undefined>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(p)) {
    if (v === undefined || v === null || v === "") continue;
    parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

export const api = {
  auth: {
    /** Public config — Google OAuth client ID + the configured allowed-domain
     *  hint + the `auth_disabled` flag (true when the dev-mode bypass is
     *  active because GOOGLE_CLIENT_ID is empty). Loaded at app boot so
     *  the AuthContext can decide whether to show the login page or
     *  auto-log-in as the synthetic dev user. */
    config: () =>
      request<{
        google_client_id: string;
        allowed_email_domains: string[];
        auth_disabled?: boolean;
      }>(`/api/auth/config`),
    /** Exchange a Google ID token (the JWT returned by GSI's callback)
     *  for an app-issued bearer token. */
    google: (credential: string) =>
      request<{
        access_token: string;
        token_type: string;
        expires_in_seconds: number;
        user: { id: string; email: string; is_active: boolean; last_login_at: string | null };
      }>(`/api/auth/google`, {
        method: "POST",
        body: JSON.stringify({ credential }),
      }),
    me: () =>
      request<{ id: string; email: string; is_active: boolean; last_login_at: string | null }>(
        `/api/auth/me`,
      ),
    logout: () =>
      request<void>(`/api/auth/logout`, { method: "POST" }),
  },
  merchants: {
    list: (params: { q?: string; limit?: number; offset?: number } = {}) =>
      request<Merchant[]>(`/api/merchants${qs(params)}`),
    get: (mid: string) => request<Merchant>(`/api/merchants/${mid}`),
  },
  easebuzz: {
    // `days` / `start_date` / `end_date` can be sent in any combination —
    // the backend resolves the active window via _resolve_window().
    list: (params: { q?: string; status?: string; delayed?: boolean;
                     days?: number; start_date?: string; end_date?: string;
                     eb_days_min?: number; eb_days_max?: number;
                     docs_sk_min?: number; docs_sk_max?: number;
                     ks_sk_min?: number; ks_sk_max?: number;
                     salt_key_start?: string; salt_key_end?: string;
                     limit?: number; offset?: number } = {}) =>
      request<EasebuzzList>(`/api/easebuzz${qs(params)}`),
    stats: (params: { q?: string; status?: string; delayed?: boolean;
                      days?: number; start_date?: string; end_date?: string;
                      eb_days_min?: number; eb_days_max?: number;
                      docs_sk_min?: number; docs_sk_max?: number;
                      ks_sk_min?: number; ks_sk_max?: number;
                      salt_key_start?: string; salt_key_end?: string;
                      include_seeded?: boolean } = {}) =>
      request<Stats>(`/api/easebuzz/stats${qs(params)}`),
    timeseries: (params: { days?: number; start_date?: string; end_date?: string } = { days: 30 }) =>
      request<TimeseriesPoint[]>(`/api/easebuzz/timeseries${qs(params)}`),
    ebTimes: (params: { days?: number; start_date?: string; end_date?: string; sla_days?: number } = { days: 30 }) =>
      request<EbTimesResponse>(`/api/easebuzz/eb-times${qs(params)}`),
    /** Seeded rows that still need kickstart OR salt&key. Backend computes
     *  the predicate in SQL so the count isn't silently capped at 200
     *  like the old client-side filter was. */
    needsReview: (params: { limit?: number } = {}) =>
      request<{ total: number; items: EasebuzzRow[] }>(
        `/api/easebuzz/needs-review${qs(params)}`,
      ),
    /** Trigger a CSV download of the filter-matching rows. Uses fetch +
     *  blob (rather than a plain anchor) so the X-API-Key header makes
     *  it onto the request — anchors can't carry custom headers. */
    exportCsv: async (params: {
      q?: string; status?: string; delayed?: boolean;
      days?: number; start_date?: string; end_date?: string;
      eb_days_min?: number; eb_days_max?: number;
      docs_sk_min?: number; docs_sk_max?: number;
      ks_sk_min?: number;   ks_sk_max?: number;
      salt_key_start?: string; salt_key_end?: string;
    } = {}) => {
      const res = await fetch(`${BASE}/api/easebuzz/export.csv${qs(params)}`, {
        headers: buildHeaders(),
      });
      if (!res.ok) throw new Error(`Export failed: ${res.status} ${res.statusText}`);
      const blob = await res.blob();
      // Pull filename out of Content-Disposition if the server set one,
      // otherwise build a sensible default with today's date.
      let filename = `easebuzz-onboarding-${new Date().toISOString().slice(0, 10)}.csv`;
      const cd = res.headers.get("content-disposition") || "";
      const m = /filename="?([^"]+)"?/.exec(cd);
      if (m) filename = m[1];
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    },
    ebTimesAnalytics: (params: { days?: number; start_date?: string; end_date?: string;
                                 sla_days?: number; merchant_size?: string } = { days: 30 }) =>
      request<EbAnalyticsResponse>(`/api/easebuzz/eb-times/analytics${qs(params)}`),
    get: (id: string) => request<EasebuzzRow>(`/api/easebuzz/${id}`),
    patch: (id: string, body: EasebuzzPatch) =>
      request<EasebuzzRow>(`/api/easebuzz/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
  },
};
