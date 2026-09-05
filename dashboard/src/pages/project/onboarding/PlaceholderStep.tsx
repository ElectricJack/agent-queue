import type { ComponentType } from "react";

export function placeholder(title: string, owner: string): ComponentType {
  function PlaceholderStep() {
    return (
      <p className="rounded-lg border border-dashed border-gray-700 p-4 text-sm text-gray-400">
        {title} — this panel is filled in by the <code className="text-gray-300">{owner}</code> task.
      </p>
    );
  }
  PlaceholderStep.displayName = `Placeholder(${title})`;
  return PlaceholderStep;
}
