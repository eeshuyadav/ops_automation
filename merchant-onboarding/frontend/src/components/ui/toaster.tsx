// Mount <Toaster /> once inside <ToastProvider> in App.tsx.
import { useEffect, useState } from "react";
import { AlertCircle, CheckCircle2, X } from "lucide-react";

import { cn } from "@/lib/utils";
import {
  useToastContext,
  type ToastRecord,
  type ToastVariant,
} from "@/components/ui/toast";

// ─────────────────────────────────────────────────────────────────────────
// Toaster
//
// Renders the live toasts in a fixed bottom-right stack. Pure presentation —
// all queue state lives in <ToastProvider> (see toast.tsx).
// ─────────────────────────────────────────────────────────────────────────

const VARIANT_STYLES: Record<ToastVariant, string> = {
  default: "border-gray-200 bg-white",
  success: "border-emerald-200 bg-emerald-50",
  error:   "border-red-200 bg-red-50",
};

function VariantIcon({ variant }: { variant: ToastVariant }) {
  if (variant === "success") {
    return <CheckCircle2 className="h-4 w-4 text-emerald-600 mt-0.5 shrink-0" />;
  }
  if (variant === "error") {
    return <AlertCircle className="h-4 w-4 text-red-600 mt-0.5 shrink-0" />;
  }
  return null;
}

/**
 * One toast row. Owns a tiny `entered` flag so we can run the slide-in
 * animation by mounting at `translate-x-full` and flipping to `translate-x-0`
 * on the next frame.
 */
function ToastItem({
  toast,
  onDismiss,
}: {
  toast: ToastRecord;
  onDismiss: (id: string) => void;
}) {
  const [entered, setEntered] = useState(false);

  useEffect(() => {
    // requestAnimationFrame ensures the initial off-screen state is committed
    // before we transition into place — otherwise the browser collapses
    // both into the same paint and the animation is skipped.
    const handle = requestAnimationFrame(() => setEntered(true));
    return () => cancelAnimationFrame(handle);
  }, []);

  return (
    <div
      role="status"
      className={cn(
        "w-80 rounded-lg border shadow-md p-4 transition-all duration-200 ease-out",
        VARIANT_STYLES[toast.variant],
        entered ? "translate-x-0 opacity-100" : "translate-x-full opacity-0",
      )}
    >
      <div className="flex items-start gap-2">
        <VariantIcon variant={toast.variant} />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-gray-900">{toast.title}</p>
          {toast.description && (
            <p className="text-xs text-gray-500 mt-1">{toast.description}</p>
          )}
        </div>
        <button
          type="button"
          onClick={() => onDismiss(toast.id)}
          aria-label="Dismiss notification"
          className="shrink-0 rounded-md p-0.5 text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-colors"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}

export function Toaster() {
  const { toasts, dismiss } = useToastContext();

  return (
    <div
      aria-live="polite"
      aria-atomic="true"
      className="fixed bottom-4 right-4 z-50 space-y-2 pointer-events-none"
    >
      {toasts.map((t) => (
        // pointer-events-auto on the child so the container itself never
        // blocks clicks elsewhere on the page.
        <div key={t.id} className="pointer-events-auto">
          <ToastItem toast={t} onDismiss={dismiss} />
        </div>
      ))}
    </div>
  );
}
