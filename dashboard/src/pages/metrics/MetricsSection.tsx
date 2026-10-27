import { useId, useState, type ReactNode } from "react";
import { ChevronDownIcon } from "@heroicons/react/24/outline";
import { readDeviceLocal, writeDeviceLocal, type MetricsSectionKey } from "../../deviceLocal";

export default function MetricsSection({ title, storageKey, children }: {
  title: string;
  storageKey: MetricsSectionKey;
  children: ReactNode;
}) {
  const id = useId();
  const [open, setOpen] = useState(() => readDeviceLocal(storageKey) !== "false");

  return (
    <section aria-labelledby={`${id}-heading`} className="space-y-3">
      <h2 id={`${id}-heading`}>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={`${id}-content`}
          onClick={() => {
            writeDeviceLocal(storageKey, String(!open));
            setOpen(!open);
          }}
          className="flex w-full items-center gap-2 rounded text-left text-sm font-semibold text-gray-200 hover:text-white focus-visible:outline focus-visible:outline-2 focus-visible:outline-indigo-400"
        >
          <ChevronDownIcon aria-hidden="true" className={`h-4 w-4 shrink-0 transition-transform ${open ? "" : "-rotate-90"}`} />
          {title}
        </button>
      </h2>
      <div id={`${id}-content`} hidden={!open}>{children}</div>
    </section>
  );
}
