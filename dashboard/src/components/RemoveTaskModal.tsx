import DeleteTaskModal from "./DeleteTaskModal";
import type { Task } from "../api/hooks";

/** Compatibility wrapper for callers migrating from the old remove action. */
export default function RemoveTaskModal({ task, onClose, onRemoved }: {
  task: Task;
  onClose: () => void;
  onRemoved?: () => void;
}) {
  return <DeleteTaskModal task={task} onClose={onClose} onDeleted={onRemoved} />;
}
