import { CpuChipIcon } from "@heroicons/react/24/outline";

export default function IntelligenceClassesStub() {
  return (
    <div className="space-y-4">
      <header>
        <h2 className="text-lg font-semibold">Intelligence Classes</h2>
        <p className="text-sm text-gray-500">
          Curated model tiers referenced by task routing. Sourced from{" "}
          <code className="rounded bg-gray-800 px-1 text-xs">
            vault/intelligence-classes/*.md
          </code>
          .
        </p>
      </header>
      <div className="flex flex-col items-center justify-center gap-3 rounded border border-dashed border-gray-700 bg-gray-900/40 p-10 text-center">
        <CpuChipIcon className="h-8 w-8 text-gray-600" />
        <p className="text-gray-400">Wiring lands with Phase 1 (control plane core).</p>
      </div>
    </div>
  );
}
