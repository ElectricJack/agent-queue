import { useId } from "react";
import type { EventGroupDTO } from "../../api/client";

export const ALL_EVENTS = "";

export interface EventScopeSelectorProps {
  groups: EventGroupDTO[];
  /** `""` is "All events". */
  value: string;
  onChange: (eventType: string) => void;
}

/** Which event's rules the canvas is showing.
 *
 *  Filtering is server-side and lossless: `event_groups` always lists every
 *  event, so the option list never depends on the current filter and an
 *  operator can always get back to a scope they narrowed away from. */
export default function EventScopeSelector({ groups, value, onChange }: EventScopeSelectorProps) {
  const id = useId();
  const total = groups.reduce((sum, group) => sum + (group.node_count ?? 0), 0);
  return (
    <div className="flex items-center gap-2">
      <label htmlFor={id} className="text-xs text-gray-400">
        Event scope
      </label>
      <select
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="rounded-md border border-gray-700 bg-gray-900 px-2 py-1 text-xs text-gray-200 focus:border-indigo-500 focus:outline-none"
      >
        <option value={ALL_EVENTS}>All events ({total} steps)</option>
        {groups.map((group) => (
          <option key={group.event_type} value={group.event_type}>
            {group.event_type} ({group.node_count ?? 0} steps, {group.edge_count ?? 0} transitions)
          </option>
        ))}
      </select>
    </div>
  );
}
