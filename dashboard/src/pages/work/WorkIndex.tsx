import { Link } from "react-router-dom";

// Placeholder — replaced in Phase 3 Task 4.
export default function WorkIndex() {
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">Work</h1>
        <p className="text-sm text-gray-500">
          Tasks, sessions, gates, and events across all projects.
        </p>
      </header>
      <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <li>
          <Link
            to="/work/events"
            className="block rounded border border-gray-800 bg-gray-900 p-4 hover:border-indigo-500/50 hover:bg-gray-800"
          >
            <p className="font-medium text-gray-200">Events</p>
            <p className="text-xs text-gray-500">Live daemon event stream.</p>
          </Link>
        </li>
        <li>
          <Link
            to="/work/sessions"
            className="block rounded border border-gray-800 bg-gray-900 p-4 hover:border-indigo-500/50 hover:bg-gray-800"
          >
            <p className="font-medium text-gray-200">Sessions</p>
            <p className="text-xs text-gray-500">Active and recent agent sessions.</p>
          </Link>
        </li>
        <li>
          <Link
            to="/work/gates"
            className="block rounded border border-gray-800 bg-gray-900 p-4 hover:border-indigo-500/50 hover:bg-gray-800"
          >
            <p className="font-medium text-gray-200">Gates</p>
            <p className="text-xs text-gray-500">Pending human gates and approvals.</p>
          </Link>
        </li>
      </ul>
    </div>
  );
}
