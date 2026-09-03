/**
 * The now-row: five numbers you read before you read any chart.
 *
 * Each tile shows the newest sample's value and, where the distinction
 * matters, what it is bounded by (slots against their cap, load against the
 * core count).  A missing reading renders as "—" rather than 0.
 */

import {
  ArrowsRightLeftIcon,
  CpuChipIcon,
  RectangleStackIcon,
  ServerStackIcon,
  UsersIcon,
} from "@heroicons/react/24/outline";
import type { MetricsSample } from "../../api/metrics";
import { pick } from "./series";

function format(value: number | null, digits = 0): string {
  if (value == null) return "—";
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function Tile({
  icon,
  label,
  value,
  hint,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="rounded-xl border border-gray-800 bg-gray-900/70 p-4">
      <div className="flex items-center gap-2 text-xs uppercase tracking-wide text-gray-500">
        {icon}
        <span>{label}</span>
      </div>
      <p className="mt-2 text-2xl font-semibold text-gray-100 tabular-nums">{value}</p>
      {hint && <p className="mt-1 text-xs text-gray-500">{hint}</p>}
    </div>
  );
}

export default function StatTiles({ sample }: { sample: MetricsSample | null }) {
  const row = (sample ?? {}) as unknown as Record<string, unknown>;
  const slotsUsed = pick(row, "slots.used");
  const slotsCap = pick(row, "slots.cap");
  const load = pick(row, "machine.load1");
  const cores = pick(row, "machine.cpu_count");
  const subagentsComplete =
    (sample?.subagents as { complete?: boolean } | undefined)?.complete !== false;

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
      <Tile
        icon={<UsersIcon className="h-4 w-4" />}
        label="Agents now"
        value={format(pick(row, "agents.total"))}
        hint={`${format(pick(row, "agents.by_state.starting"))} starting`}
      />
      <Tile
        icon={<ArrowsRightLeftIcon className="h-4 w-4" />}
        label="Sub-agents / hr"
        // Started over the window, not open right now: pool sessions exit
        // before you look at them, so the instantaneous count reads 0 on a
        // fleet that is visibly busy.
        value={format(pick(row, "subagents.spawned_per_hour"))}
        // A live session launched without its harness hooks makes the open
        // count a floor; saying so beats a confident wrong number.
        hint={
          subagentsComplete
            ? `${format(pick(row, "subagents.active"))} open now`
            : `${format(pick(row, "subagents.active"))}+ open — hooks missing`
        }
      />
      <Tile
        icon={<ServerStackIcon className="h-4 w-4" />}
        label="Tokens / min"
        value={format(pick(row, "tokens.total_per_min"))}
        hint={`${format(pick(row, "tokens.input_per_min"))} in · ${format(
          pick(row, "tokens.output_per_min"),
        )} out · ${format(pick(row, "tokens.cache_read_per_min"))} cached`}
      />
      <Tile
        icon={<RectangleStackIcon className="h-4 w-4" />}
        label="Slots used"
        value={format(slotsUsed)}
        hint={slotsCap == null ? "no cap (worktrees off)" : `of ${format(slotsCap)} cap`}
      />
      <Tile
        icon={<CpuChipIcon className="h-4 w-4" />}
        label="Load"
        value={format(load, 2)}
        hint={cores == null ? undefined : `${format(cores)} cores`}
      />
    </div>
  );
}
