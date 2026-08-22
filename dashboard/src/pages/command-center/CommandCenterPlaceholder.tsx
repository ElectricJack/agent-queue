import { Link } from "react-router-dom";
import { Squares2X2Icon } from "@heroicons/react/24/outline";

export default function CommandCenterPlaceholder() {
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">Command Center</h1>
        <p className="text-sm text-gray-500">
          Live pan/zoom work-graph canvas — Phase 4.
        </p>
      </header>
      <div className="flex flex-col items-center justify-center gap-3 rounded border border-dashed border-gray-700 bg-gray-900/40 p-12 text-center">
        <Squares2X2Icon className="h-10 w-10 text-gray-600" />
        <p className="text-gray-400">Coming in Phase 4.</p>
        <p className="text-sm text-gray-500">
          Until then, use{" "}
          <Link to="/" className="text-indigo-400 hover:underline">
            Chat
          </Link>{" "}
          to talk to the supervisor or{" "}
          <Link to="/work" className="text-indigo-400 hover:underline">
            Work
          </Link>{" "}
          to see tasks and agents.
        </p>
      </div>
    </div>
  );
}
