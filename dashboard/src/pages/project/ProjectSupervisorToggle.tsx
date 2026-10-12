import { useId } from "react";

export default function ProjectSupervisorToggle({ enabled, onChange }: {
  enabled: boolean;
  onChange: (enabled: boolean) => void;
}) {
  const hintId = useId();
  return (
    <div className="space-y-1 text-sm">
      <label className="flex items-center gap-2 text-gray-200">
        <input
          type="checkbox"
          role="switch"
          checked={enabled}
          onChange={(event) => onChange(event.target.checked)}
          aria-describedby={hintId}
        />
        Project supervisor
      </label>
      <p id={hintId} className="text-xs text-gray-500">
        When off, the global supervisor handles this project.
      </p>
    </div>
  );
}
