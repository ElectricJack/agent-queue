import { useEffect, useMemo } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ChatBubbleLeftRightIcon } from "@heroicons/react/24/outline";
import { useProjects } from "../../api/hooks";

export default function ChatLanding() {
  const { data: projects, isLoading } = useProjects();
  const navigate = useNavigate();
  const list = useMemo(() => projects ?? [], [projects]);

  // Auto-forward when there's exactly one project — a single-project user
  // never wants to pick from a list of one.
  useEffect(() => {
    if (!isLoading && list.length === 1 && list[0]) {
      navigate(`/chat/${list[0].id}`, { replace: true });
    }
  }, [isLoading, list, navigate]);

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-bold">Chat</h1>
        <p className="text-sm text-gray-500">
          Pick a project to talk to its supervisor.
        </p>
      </header>
      {isLoading && <p className="text-sm text-gray-500">Loading projects…</p>}
      {!isLoading && list.length === 0 && (
        <p className="text-sm text-gray-500">
          No projects yet. Create one in{" "}
          <Link to="/settings/config" className="text-indigo-400 hover:underline">
            Settings
          </Link>
          .
        </p>
      )}
      <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {list.map((p) => (
          <li key={p.id}>
            <Link
              to={`/chat/${p.id}`}
              className="flex items-center gap-3 rounded border border-gray-800 bg-gray-900 p-4 hover:border-indigo-500/50 hover:bg-gray-800"
            >
              <ChatBubbleLeftRightIcon className="h-6 w-6 text-indigo-400" />
              <div>
                <p className="font-medium text-gray-200">{p.name || p.id}</p>
                <p className="font-mono text-xs text-gray-500">supervisor-{p.id}</p>
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
