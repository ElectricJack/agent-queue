import { Link } from "react-router-dom";
import { useFocusChrome } from "./focusChrome";
import { useFocusBack } from "./useFocusBack";

const BUTTON = "inline-flex items-center justify-center rounded-md px-4 text-sm font-medium";

/** A focus page without focus content yet: say so and link the full dashboard. */
export function FocusUnavailable({ title, fullHref }: { title: string; fullHref: string }) {
  useFocusChrome({ title, fullHref });
  return (
    <div className="space-y-3 p-4 text-sm text-gray-300">
      <p>This view has no focus layout yet.</p>
      <Link to={fullHref} data-primary-control className={`${BUTTON} bg-indigo-600 text-white`}>
        Open in the full dashboard
      </Link>
    </div>
  );
}

/** A scoped error: what failed, Retry when it can help, and a way back. */
export function FocusError({ title, message, onRetry }: { title: string; message: string; onRetry?: () => void }) {
  useFocusChrome({ title });
  const back = useFocusBack();
  return (
    <div role="alert" className="space-y-3 p-4">
      <p className="break-words text-sm text-red-200">{message}</p>
      <div className="flex flex-wrap gap-2">
        {onRetry && (
          <button type="button" data-primary-control onClick={onRetry} className={`${BUTTON} border border-gray-700 text-gray-200`}>
            Retry
          </button>
        )}
        <button type="button" data-primary-control onClick={back} className={`${BUTTON} border border-gray-700 text-gray-200`}>
          Go back
        </button>
      </div>
    </div>
  );
}
