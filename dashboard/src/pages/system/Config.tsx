import { Cog6ToothIcon } from "@heroicons/react/24/outline";

export default function SystemConfig() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">System Config</h1>
      <div className="rounded-lg border border-gray-800 bg-gray-900 p-8 text-center">
        <Cog6ToothIcon className="mx-auto h-10 w-10 text-gray-600" />
        <p className="mt-3 text-sm text-gray-400">
          System-wide configuration UI is not wired yet.
        </p>
        <p className="mt-1 text-xs text-gray-500">
          Edit <code className="rounded bg-gray-800 px-1 py-0.5">~/.agent-queue/config.yaml</code>{" "}
          and run <code className="rounded bg-gray-800 px-1 py-0.5">/system/reload-config</code>.
        </p>
      </div>
    </div>
  );
}
