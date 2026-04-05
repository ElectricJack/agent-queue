const statusColors: Record<string, string> = {
  running: "bg-green-500/10 text-green-400",
  idle: "bg-gray-500/10 text-gray-400",
  busy: "bg-yellow-500/10 text-yellow-400",
  offline: "bg-red-500/10 text-red-400",
  ready: "bg-blue-500/10 text-blue-400",
  in_progress: "bg-yellow-500/10 text-yellow-400",
  completed: "bg-green-500/10 text-green-400",
  failed: "bg-red-500/10 text-red-400",
  blocked: "bg-orange-500/10 text-orange-400",
  awaiting_approval: "bg-purple-500/10 text-purple-400",
  waiting_input: "bg-cyan-500/10 text-cyan-400",
};

export default function StatusBadge({ status }: { status?: string | null }) {
  const s = status ?? "unknown";
  const colors = statusColors[s.toLowerCase()] ?? "bg-gray-500/10 text-gray-400";
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${colors}`}>
      {s.replace(/_/g, " ")}
    </span>
  );
}
