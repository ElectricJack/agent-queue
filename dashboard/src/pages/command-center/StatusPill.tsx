import type { StatusPillModel } from "./statusPillModel";

export function StatusPill({ model, className = "" }: { model: StatusPillModel; className?: string }) {
  return (
    <span
      className={`inline-flex h-5 min-w-0 items-center gap-1.5 whitespace-nowrap rounded-md px-2 text-[11px] font-semibold ${model.treatment.pillClass} ${className}`}
      title={model.title}
    >
      {model.working && <span aria-hidden className="aq-pulse h-1.5 w-1.5 shrink-0 rounded-full bg-current" />}
      <span className="truncate">{model.text}</span>
    </span>
  );
}
