// Auth state stored in localStorage and read once at app boot.
//
// The backend uses stateless JWTs — token lives entirely on the client
// (localStorage) and is sent as `Authorization: Bearer <token>` on every
// request. Logout clears the key; expired tokens trigger a 401 in
// `lib/api.ts` which redirects to /login.
//
// Why localStorage (vs httpOnly cookie): user accepted the tradeoff in
// the auth design — internal dashboard behind VPN, XSS risk is low,
// avoids the CSRF + CORS complexity of cookie-based auth.

const TOKEN_KEY = "moa.auth.token";
const USER_KEY  = "moa.auth.user";

export interface AuthUser {
  id: string;
  email: string;
  is_active: boolean;
  last_login_at: string | null;
}

export function readToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function readUser(): AuthUser | null {
  try {
    const raw = window.localStorage.getItem(USER_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as AuthUser;
  } catch {
    return null;
  }
}

export function writeAuth(token: string, user: AuthUser): void {
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
    window.localStorage.setItem(USER_KEY, JSON.stringify(user));
  } catch {
    /* best-effort */
  }
}

export function clearAuth(): void {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
    window.localStorage.removeItem(USER_KEY);
  } catch {
    /* best-effort */
  }
}
