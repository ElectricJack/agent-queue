import { useState } from "react";
import { CheckIcon, ClipboardIcon } from "@heroicons/react/24/outline";

/** Copies a task id to the clipboard and shows a brief confirmation. Stops
 *  propagation so it can be nested inside a row/card that opens the task. */
export function CopyTaskIdButton({ taskId, className = "" }: { taskId: string; className?: string }) {
  const [copied, setCopied] = useState(false);

  return (
    <button
      type="button"
      aria-label={`Copy task id ${taskId}`}
      title="Copy task id"
      className={`shrink-0 rounded p-0.5 text-gray-500 hover:bg-gray-700 hover:text-gray-200 ${className}`}
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
