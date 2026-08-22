import { Link } from "react-router-dom";
import {
  PlayCircleIcon,
  CheckCircleIcon,
  XCircleIcon,
  LockClosedIcon,
  LockOpenIcon,
  ExclamationTriangleIcon,
} from "@heroicons/react/24/outline";
import type { NotifyEvent } from "../../ws/types";

function fmt(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString();
}

export default function InlineEventCard({ event, ts }: { event: NotifyEvent; ts: number }) {
  const time = fmt(ts);
  switch (event.event_type) {
    case "notify.task_started":
      return (
        <Row icon={<PlayCircleIcon className="h-4 w-4 text-blue-400" />} time={time}>
          Task started:{" "}
          <Link className="text-indigo-400 hover:underline" to={`/tasks/${event.task.id}`}>
            {event.task.title}
          </Link>
        </Row>
      );
    case "notify.task_completed":
      return (
        <Row icon={<CheckCircleIcon className="h-4 w-4 text-emerald-400" />} time={time}>
          Completed:{" "}
          <Link className="text-indigo-400 hover:underline" to={`/tasks/${event.task.id}`}>
            {event.task.title}
          </Link>
        </Row>
      );
    case "notify.task_failed":
      return (
        <Row icon={<XCircleIcon className="h-4 w-4 text-red-400" />} time={time}>
          Failed:{" "}
          <Link className="text-indigo-400 hover:underline" to={`/tasks/${event.task.id}`}>
            {event.task.title}
          </Link>
          <span className="text-gray-500"> — {event.error_label}</span>
        </Row>
      );
    case "gate.created":
      return (
        <Row icon={<LockClosedIcon className="h-4 w-4 text-amber-400" />} time={time}>
          Gate: {event.gate_type} — {event.title}
        </Row>
      );
    case "gate.resolved":
      return (
        <Row icon={<LockOpenIcon className="h-4 w-4 text-emerald-400" />} time={time}>
          Gate resolved by {event.resolved_by}
        </Row>
      );
    case "notify.playbook_run_failed":
      return (
        <Row icon={<ExclamationTriangleIcon className="h-4 w-4 text-red-400" />} time={time}>
          Playbook{" "}
          <Link className="text-indigo-400 hover:underline" to={`/playbooks/${event.playbook_id}`}>
            {event.playbook_id}
          </Link>{" "}
          failed at {event.failed_at_node}
        </Row>
      );
    default:
      return null;
  }
}

function Row({
  icon,
  time,
  children,
}: {
  icon: React.ReactNode;
  time: string;
  children: React.ReactNode;
}) {
  return (
    <div className="my-1 flex items-center gap-2 rounded border border-gray-800/60 bg-gray-900/40 px-2 py-1 text-xs text-gray-300">
      {icon}
      <span className="flex-1">{children}</span>
      <span className="text-gray-600">{time}</span>
    </div>
  );
}
