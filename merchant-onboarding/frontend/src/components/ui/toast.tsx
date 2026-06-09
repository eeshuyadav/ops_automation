import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

// ─────────────────────────────────────────────────────────────────────────
// Toast: context + hook
//
// Minimal, dependency-free notification system. <ToastProvider> owns the
// queue and exposes `useToast()` for callers to push new toasts. The
// matching <Toaster /> (see toaster.tsx) reads from this context and
// renders the actual UI. Keeping them in separate files lets the provider
// sit very high in the tree without dragging icon imports along with it.
// ─────────────────────────────────────────────────────────────────────────

export type ToastVariant = "default" | "success" | "error";

export interface ToastOptions {
  title: string;
  description?: string;
  variant?: ToastVariant;
}

export interface ToastRecord {
  id: string;
  title: string;
  description?: string;
  variant: ToastVariant;
}

interface ToastContextValue {
  /** Active toasts, oldest first. <Toaster /> renders these directly. */
  toasts: ToastRecord[];
  /** Push a new toast onto the queue. Returns the auto-generated id. */
  toast: (opts: ToastOptions) => string;
  /** Manually dismiss a toast (used by the × button). */
  dismiss: (id: string) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

/** Auto-dismiss interval in ms. */
const TOAST_DURATION_MS = 5_000;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastRecord[]>([]);

  // Track outstanding setTimeout handles so we can clear them on unmount or
  // manual dismissal — avoids state updates on an unmounted tree.
  const timers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());

  const dismiss = useCallback((id: string) => {
    const handle = timers.current.get(id);
    if (handle !== undefined) {
      clearTimeout(handle);
      timers.current.delete(id);
    }
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const toast = useCallback(
    (opts: ToastOptions): string => {
      const id =
        typeof crypto !== "undefined" && "randomUUID" in crypto
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(36).slice(2)}`;

      const record: ToastRecord = {
        id,
        title: opts.title,
        description: opts.description,
        variant: opts.variant ?? "default",
      };
      setToasts((prev) => [...prev, record]);

      const handle = setTimeout(() => {
        timers.current.delete(id);
        setToasts((prev) => prev.filter((t) => t.id !== id));
      }, TOAST_DURATION_MS);
      timers.current.set(id, handle);

      return id;
    },
    [],
  );

  // Cleanup every outstanding timer when the provider unmounts.
  useEffect(() => {
    const map = timers.current;
    return () => {
      map.forEach((handle) => clearTimeout(handle));
      map.clear();
    };
  }, []);

  const value = useMemo<ToastContextValue>(
    () => ({ toasts, toast, dismiss }),
    [toasts, toast, dismiss],
  );

  return <ToastContext.Provider value={value}>{children}</ToastContext.Provider>;
}

/**
 * Read-only access to the queue. Internal — consumers should use
 * `useToast()` which only exposes the safe push API.
 */
export function useToastContext(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    throw new Error("useToast / <Toaster /> must be used inside <ToastProvider>.");
  }
  return ctx;
}

/**
 * Public hook: `const { toast } = useToast(); toast({ title: "Saved" })`.
 */
export function useToast(): { toast: (opts: ToastOptions) => void } {
  const { toast } = useToastContext();
  return { toast: (opts) => void toast(opts) };
}
