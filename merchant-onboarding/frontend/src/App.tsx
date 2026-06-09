import { lazy, Suspense, useEffect, useState } from "react";
import {
  Link,
  NavLink,
  Navigate,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import {
  Activity,
  LayoutDashboard,
  ListChecks,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  Timer,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { ToastProvider } from "@/components/ui/toast";
import { Toaster } from "@/components/ui/toaster";
import { AuthProvider, useAuth } from "@/contexts/AuthContext";

import DashboardPage from "@/pages/Dashboard";
import OnboardingPage from "@/pages/Onboarding";
import LoginPage from "@/pages/Login";
// Lazy-load the chart-heavy detail page so the Onboarding table page doesn't
// pay the ~150KB recharts bundle tax it never uses.
const EbTimeDetail = lazy(() => import("@/pages/EbTimeDetail"));

const nav = [
  { to: "/dashboard",     label: "Dashboard",           icon: LayoutDashboard },
  { to: "/onboarding",    label: "Easebuzz Onboarding", icon: ListChecks },
  { to: "/eb-time",       label: "Easebuzz Speed",      icon: Timer },
];

const STORAGE_KEY = "moa.sidebar.open";

/** localStorage access wrapped in try/catch because Safari private-browsing
 *  and Chrome with cookies blocked both throw on `getItem` / `setItem`. */
function readSidebarOpen(): boolean {
  try {
    if (typeof window === "undefined") return true;
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return stored === null ? true : stored === "1";
  } catch {
    return true;
  }
}
function writeSidebarOpen(open: boolean): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, open ? "1" : "0");
  } catch {
    /* best-effort — swallowing is fine, state is already in React */
  }
}

/** Wraps protected routes — redirects to /login if the user isn't
 *  authenticated. While the AuthContext is hydrating the stored token
 *  (verifying via /me) we render nothing so the app shell doesn't flash
 *  to logged-out and back. */
function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { user, initializing } = useAuth();
  const location = useLocation();
  if (initializing) {
    return <div className="min-h-screen flex items-center justify-center text-sm text-gray-500">Loading…</div>;
  }
  if (!user) {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?next=${next}`} replace />;
  }
  return <>{children}</>;
}

export default function App() {
  return (
    <ToastProvider>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route
            path="/*"
            element={
              <ProtectedRoute>
                <AppShell />
              </ProtectedRoute>
            }
          />
        </Routes>
      </AuthProvider>
      <Toaster />
    </ToastProvider>
  );
}

function AppShell() {
  const [open, setOpen] = useState<boolean>(readSidebarOpen);
  useEffect(() => { writeSidebarOpen(open); }, [open]);
  const { user, logout } = useAuth();

  return (
    <>
      <div className="flex h-screen">
      {/* Sidebar — animated width, fully collapses to 0 when closed */}
      <aside
        className={cn(
          "shrink-0 bg-gray-900 text-white flex flex-col overflow-hidden transition-[width] duration-200 ease-in-out",
          open ? "w-60" : "w-0",
        )}
      >
        <div className="px-6 py-5 border-b border-gray-800">
          <Link to="/dashboard" className="flex items-center gap-2 whitespace-nowrap">
            <Activity className="h-5 w-5 text-brand-400 shrink-0" />
            <span className="text-sm font-semibold">Merchant Onboarding</span>
          </Link>
        </div>
        <nav className="flex-1 p-3 space-y-1">
          {nav.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-3 px-3 py-2 rounded-md text-sm transition-colors whitespace-nowrap",
                  isActive
                    ? "bg-brand-600 text-white"
                    : "text-gray-300 hover:bg-gray-800 hover:text-white",
                )
              }
            >
              <n.icon className="h-4 w-4 shrink-0" />
              {n.label}
            </NavLink>
          ))}
        </nav>
        {/* User chip + actions, sticky to the bottom of the sidebar. */}
        <div className="p-3 border-t border-gray-800 space-y-1">
          {user && (
            <div
              className="px-3 py-2 rounded-md text-xs text-gray-400 whitespace-nowrap"
              title={user.email}
            >
              Signed in as
              <div className="text-gray-100 font-medium truncate">{user.email}</div>
            </div>
          )}
          <button
            type="button"
            onClick={() => { void logout(); }}
            className="w-full flex items-center gap-2 px-3 py-2 rounded-md text-sm text-gray-300 hover:bg-gray-800 hover:text-white transition-colors whitespace-nowrap"
          >
            <LogOut className="h-4 w-4 shrink-0" />
            Sign out
          </button>
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="w-full flex items-center gap-2 px-3 py-2 rounded-md text-sm text-gray-300 hover:bg-gray-800 hover:text-white transition-colors whitespace-nowrap"
            aria-label="Collapse sidebar"
            title="Collapse sidebar"
          >
            <PanelLeftClose className="h-4 w-4 shrink-0" />
            Collapse
          </button>
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-auto relative">
        {!open && (
          <button
            type="button"
            onClick={() => setOpen(true)}
            className="absolute top-4 left-4 z-10 inline-flex items-center gap-2 px-2.5 py-1.5 rounded-md border border-gray-200 bg-white shadow-sm text-gray-700 hover:bg-gray-50 transition-colors"
            aria-label="Open sidebar"
            title="Open sidebar"
          >
            <PanelLeftOpen className="h-4 w-4" />
            <span className="text-xs font-medium">Menu</span>
          </button>
        )}
        <div className={cn(!open && "pt-12")}>
          <Suspense fallback={
            <div className="p-8 text-sm text-gray-500">Loading…</div>
          }>
            <Routes>
              <Route path="/" element={<Navigate to="/dashboard" replace />} />
              <Route path="/dashboard" element={<DashboardPage />} />
              <Route path="/onboarding" element={<OnboardingPage />} />
              <Route path="/eb-time" element={<EbTimeDetail />} />
              <Route path="*" element={<Navigate to="/dashboard" replace />} />
            </Routes>
          </Suspense>
        </div>
      </main>
      </div>
    </>
  );
}
