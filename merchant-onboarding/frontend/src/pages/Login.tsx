import { useEffect, useRef, useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { Activity } from "lucide-react";

import { useAuth } from "@/contexts/AuthContext";
import { api } from "@/lib/api";

// Google Identity Services script (loaded once at app startup if not
// already present). The library injects `window.google.accounts.id`.
const GSI_SRC = "https://accounts.google.com/gsi/client";

declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (opts: GsiInitOptions) => void;
          renderButton: (parent: HTMLElement, opts: GsiButtonOptions) => void;
        };
      };
    };
  }
}

interface GsiInitOptions {
  client_id: string;
  callback: (resp: { credential: string }) => void;
  auto_select?: boolean;
  hd?: string;             // hosted-domain restriction (Workspace hint)
  ux_mode?: "popup" | "redirect";
  context?: "signin" | "signup" | "use";
}
interface GsiButtonOptions {
  type?: "standard" | "icon";
  theme?: "outline" | "filled_blue" | "filled_black";
  size?: "large" | "medium" | "small";
  width?: number;
  text?: "signin_with" | "signup_with" | "continue_with" | "signin";
  shape?: "rectangular" | "pill" | "circle" | "square";
}

function loadGsiScript(): Promise<void> {
  return new Promise((resolve, reject) => {
    if (typeof window === "undefined") return reject(new Error("no window"));
    if (window.google?.accounts?.id) return resolve();
    const existing = document.querySelector<HTMLScriptElement>(
      `script[src="${GSI_SRC}"]`,
    );
    if (existing) {
      existing.addEventListener("load", () => resolve(), { once: true });
      existing.addEventListener("error", () => reject(new Error("GSI failed to load")), { once: true });
      return;
    }
    const s = document.createElement("script");
    s.src = GSI_SRC;
    s.async = true;
    s.defer = true;
    s.onload = () => resolve();
    s.onerror = () => reject(new Error("GSI failed to load"));
    document.head.appendChild(s);
  });
}

export default function LoginPage() {
  const { user, initializing, signInWithGoogleCredential } = useAuth();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const next = params.get("next") || "/dashboard";

  const buttonRef = useRef<HTMLDivElement | null>(null);
  const [clientId, setClientId] = useState<string | null>(null);
  const [allowedDomains, setAllowedDomains] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Pull the Google Client ID + allowed-domain hint from /api/auth/config
  // so it's centralized server-side (no need to rebuild the frontend when
  // rotating the OAuth client).
  useEffect(() => {
    (async () => {
      try {
        const cfg = await api.auth.config();
        setClientId(cfg.google_client_id || null);
        setAllowedDomains(cfg.allowed_email_domains || []);
      } catch (e) {
        setError((e as Error).message);
      }
    })();
  }, []);

  // Render the Google button once we have the client_id AND the GSI
  // script is loaded.
  useEffect(() => {
    if (!clientId || !buttonRef.current) return;
    let cancelled = false;
    loadGsiScript()
      .then(() => {
        if (cancelled || !buttonRef.current || !window.google) return;
        window.google.accounts.id.initialize({
          client_id: clientId,
          callback: async (resp) => {
            if (!resp?.credential) {
              setError("Google didn't return a credential. Try again.");
              return;
            }
            setSubmitting(true);
            setError(null);
            try {
              await signInWithGoogleCredential(resp.credential);
              navigate(next, { replace: true });
            } catch (e) {
              setError((e as Error).message || "Sign-in failed");
            } finally {
              setSubmitting(false);
            }
          },
          // Hosted-domain hint — Workspace prefills only @<hd> accounts in
          // the One Tap chooser. Backend still verifies independently.
          hd: allowedDomains[0]?.replace(/^@/, "") || undefined,
          auto_select: false,
          ux_mode: "popup",
        });
        window.google.accounts.id.renderButton(buttonRef.current, {
          theme: "outline",
          size: "large",
          shape: "rectangular",
          width: 280,
          text: "signin_with",
        });
      })
      .catch((e) => setError(e.message));
    return () => { cancelled = true; };
  }, [clientId, allowedDomains, next, navigate, signInWithGoogleCredential]);

  // Already logged in → straight to the destination. Wait for hydration
  // to avoid flashing the login form.
  if (!initializing && user) {
    return <Navigate to={next} replace />;
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50 px-4">
      <div className="w-full max-w-sm">
        <div className="flex items-center justify-center mb-6">
          <div className="h-10 w-10 rounded-md bg-brand-600 text-white flex items-center justify-center">
            <Activity className="h-5 w-5" />
          </div>
          <span className="ml-3 text-lg font-semibold text-gray-900">
            Merchant Onboarding
          </span>
        </div>

        <div className="rounded-xl border border-gray-200 bg-white shadow-sm p-6 space-y-5">
          <header>
            <h1 className="text-lg font-semibold text-gray-900">Sign in</h1>
            <p className="text-sm text-gray-500 mt-1">
              Use your{" "}
              <span className="font-medium text-gray-700">
                {allowedDomains[0] || "@gokwik.co"}
              </span>{" "}
              Google account to access the dashboard.
            </p>
          </header>

          {!clientId && !error && (
            <p className="text-sm text-gray-400">Loading sign-in…</p>
          )}

          {clientId && (
            <div className="flex justify-center py-2" aria-busy={submitting}>
              <div ref={buttonRef} />
            </div>
          )}

          {submitting && (
            <p className="text-sm text-gray-500 text-center">Signing you in…</p>
          )}

          {error && (
            <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
              {error}
            </div>
          )}

          <p className="text-xs text-gray-500 pt-2 border-t border-gray-100">
            Only accounts on the configured allow-list can sign in.
            If your sign-in is rejected, ask an admin to add you.
          </p>
        </div>
      </div>
    </div>
  );
}
