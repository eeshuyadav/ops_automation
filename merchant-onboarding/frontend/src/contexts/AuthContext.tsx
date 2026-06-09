import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { api } from "@/lib/api";
import { type AuthUser, clearAuth, readToken, readUser, writeAuth } from "@/lib/auth";

interface AuthCtx {
  user: AuthUser | null;
  /** Initial hydration in flight (verifying the stored token via /me). */
  initializing: boolean;
  /** Exchange a Google ID-token JWT (from GSI) for our session token. */
  signInWithGoogleCredential: (credential: string) => Promise<void>;
  logout: () => Promise<void>;
}

const Ctx = createContext<AuthCtx | undefined>(undefined);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(() => readUser());
  const [initializing, setInitializing] = useState<boolean>(() => !!readToken());

  // Hydrate on mount. Three paths:
  //   1. Backend reports `auth_disabled` (dev bypass — Client ID not yet
  //      set on the server) → fetch the synthetic user from /me and treat
  //      it like a normal session. Skip the login page entirely.
  //   2. We already have a stored token → verify it via /me.
  //   3. No token and auth required → drop straight to the login page.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const cfg = await api.auth.config();
        if (cancelled) return;
        if (cfg.auth_disabled) {
          // Dev bypass active — /api/auth/me returns the synthetic
          // _DEV_USER without needing a token, so we just use it.
          try {
            const u = await api.auth.me();
            if (!cancelled) setUser(u as AuthUser);
          } catch {
            /* even if /me hiccups, leave initializing=false so the
               ProtectedRoute can decide what to do */
          }
          if (!cancelled) setInitializing(false);
          return;
        }
      } catch {
        // /config shouldn't fail, but if it does, fall through to the
        // normal token path so the user can still log in.
      }

      if (!readToken()) {
        if (!cancelled) setInitializing(false);
        return;
      }
      try {
        const u = await api.auth.me();
        if (!cancelled) setUser(u as AuthUser);
      } catch {
        if (!cancelled) {
          clearAuth();
          setUser(null);
        }
      } finally {
        if (!cancelled) setInitializing(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const signInWithGoogleCredential = useCallback(async (credential: string) => {
    const resp = await api.auth.google(credential);
    writeAuth(resp.access_token, resp.user as AuthUser);
    setUser(resp.user as AuthUser);
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.auth.logout();
    } catch {
      /* server-side logout is best-effort; we still clear locally */
    }
    clearAuth();
    setUser(null);
  }, []);

  const value = useMemo<AuthCtx>(
    () => ({ user, initializing, signInWithGoogleCredential, logout }),
    [user, initializing, signInWithGoogleCredential, logout],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
