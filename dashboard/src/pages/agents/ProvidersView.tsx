import { useState } from "react";
import { useProviderAllocation } from "../../api/providerAllocation";
import { providerErrorText } from "../../api/providers";
import ProviderAllocationDrawer from "./ProviderAllocationDrawer";
import ProviderCard from "./ProviderCard";
import { plural } from "./allocation";

/**
 * The Providers view: one card per provider beside the pool directory, and
 * the two-step preview/apply drawer that changes a provider's worker profiles
 * in bulk (provider-worker-allocation-controls spec §Dashboard).
 *
 * A provider allocation is a batch over the existing global profiles and pool
 * controls, never a provider scheduler, so nothing here is new capacity: the
 * cards read ``provider_allocation_status`` and the drawer sends back only a
 * reviewed preview's token.
 */
export default function ProvidersView() {
  const status = useProviderAllocation();
  const [editing, setEditing] = useState<string | null>(null);
  const providers = status.data?.providers ?? [];
  const projects = status.data?.projects ?? [];
  const diagnostics = status.data?.diagnostics ?? [];
  const group = providers.find((item) => item.provider === editing);

  return (
    <section aria-label="Providers" className="flex shrink-0 flex-col gap-3">
      <div>
        <p className="text-xs text-gray-500">
          Move worker profiles between providers in bulk: preview what changes, then apply exactly that.
          Bounds stay per profile; each pool's own view keeps its surgical controls.
        </p>
        {status.data?.redacted && (
          <p className="mt-1 text-[10px] text-gray-500">Other projects' sessions and tasks are outside your scope and hidden.</p>
        )}
      </div>
      {status.error && (
        <div role="alert" className="rounded border border-red-900 bg-red-950/30 p-3 text-sm text-red-300">
          Could not load provider allocation: {providerErrorText(status.error)}{" "}
          <button type="button" className="underline" onClick={() => void status.refetch()}>Retry</button>
        </div>
      )}
      {status.isLoading && <p className="text-xs text-gray-500">Loading providers…</p>}
      {!status.isLoading && !status.error && providers.length === 0 && (
        <p className="text-xs text-gray-500">No ordinary worker profiles resolve to a known provider.</p>
      )}
      <div className="grid gap-3 lg:grid-cols-2">
        {providers.map((item) => (
          <ProviderCard key={item.provider} group={item} projects={projects} onEdit={() => setEditing(item.provider)} />
        ))}
      </div>
      {diagnostics.length > 0 && (
        <details className="rounded border border-gray-800 px-3 py-2 text-xs text-gray-400">
          <summary className="cursor-pointer">{plural(diagnostics.length, "profile")} not managed by bulk allocation</summary>
          <ul className="mt-2 space-y-0.5 font-mono text-[10px] text-gray-500">
            {diagnostics.map((item) => (
              <li key={item.kind + ":" + item.id}>
                {item.id} · {item.reason.replace(/_/g, " ")}{item.provider ? " · " + item.provider : ""}
              </li>
            ))}
          </ul>
        </details>
      )}
      {group && (
        <ProviderAllocationDrawer group={group} projects={projects} onClose={() => setEditing(null)} />
      )}
    </section>
  );
}
