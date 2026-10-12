import { useState } from "react";
import { CheckIcon, ClipboardIcon } from "@heroicons/react/24/outline";

const DEFAULT_TONE = "text-gray-500 hover:bg-gray-700 hover:text-gray-200";

/** Copies a task id to the clipboard and shows a brief confirmation. Stops
 *  propagation so it can be nested inside a row/card that opens the task.
 *  `toneClass` replaces the gray tone rather than adding to it: two colour
 *  utilities on one element resolve by stylesheet order, not class order. */
export function CopyTaskIdButton({ taskId, className = "", toneClass = DEFAULT_TONE }: {
  taskId: string;
  className?: string;
  toneClass?: string;
}) {
  const [copied, setCopied] = useState(false);

  return (
    <button
      type="button"
      aria-label={`Copy task id ${taskId}`}
      title="Copy task id"
      className={`shrink-0 rounded p-0.5 ${toneClass} ${className}`}
      onClick={(event) => {
        event.stopPropagation();
        void navigator.clipboard.writeText(taskId);
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      }}
    >
      {copied ? <CheckIcon aria-hidden className="h-3 w-3" /> : <ClipboardIcon aria-hidden className="h-3 w-3" />}
    </button>
  );
}
