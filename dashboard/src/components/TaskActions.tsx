import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  StopIcon,
  ArrowPathIcon,
  ForwardIcon,
  CheckIcon,
  DocumentCheckIcon,
  DocumentMinusIcon,
  TrashIcon,
  ChatBubbleLeftIcon,
  ArrowUturnLeftIcon,
} from "@heroicons/react/24/outline";
import type { Task } from "../api/hooks";
import {
  useStopTask,
  useRestartTask,
  useSkipTask,
  useApproveTask,
  useApprovePlan,
  useRejectPlan,
  useDeletePlan,
  useReopenWithFeedback,
  useDeleteTask,
  useProvideInput,
} from "../api/hooks";
import Modal from "./Modal";

interface TaskActionsProps {
  task: Task;
}

type ModalType = "reject-plan" | "reopen" | "answer" | "delete" | null;

export default function TaskActions({ task }: TaskActionsProps) {
  const navigate = useNavigate();
  const [modal, setModal] = useState<ModalType>(null);
  const [textInput, setTextInput] = useState("");

  const stopTask = useStopTask();
  const restartTask = useRestartTask();
  const skipTask = useSkipTask();
  const approveTask = useApproveTask();
  const approvePlan = useApprovePlan();
  const rejectPlan = useRejectPlan();
  const deletePlan = useDeletePlan();
  const reopenWithFeedback = useReopenWithFeedback();
  const deleteTask = useDeleteTask();
  const provideInput = useProvideInput();

  const isPending =
    stopTask.isPending ||
    restartTask.isPending ||
    skipTask.isPending ||
    approveTask.isPending ||
    approvePlan.isPending ||
    rejectPlan.isPending ||
    deletePlan.isPending ||
    reopenWithFeedback.isPending ||
    deleteTask.isPending ||
    provideInput.isPending;

  const s = task.status?.toUpperCase() ?? "";

  const openModal = (type: ModalType) => {
    setTextInput("");
    setModal(type);
  };

  const closeModal = () => setModal(null);

  const handleSubmitModal = () => {
    if (modal === "reject-plan") {
      rejectPlan.mutate({ task_id: task.id, feedback: textInput }, { onSuccess: closeModal });
    } else if (modal === "reopen") {
      reopenWithFeedback.mutate({ task_id: task.id, feedback: textInput }, { onSuccess: closeModal });
    } else if (modal === "answer") {
      provideInput.mutate({ task_id: task.id, input: textInput }, { onSuccess: closeModal });
    } else if (modal === "delete") {
      deleteTask.mutate(
        { task_id: task.id },
        { onSuccess: () => { closeModal(); navigate("/tasks"); } },
      );
    }
  };

  const buttons: { label: string; icon: React.ReactNode; onClick: () => void; variant: string; show: boolean }[] = [
    {
      label: "Stop",
      icon: <StopIcon className="h-3.5 w-3.5" />,
      onClick: () => stopTask.mutate({ task_id: task.id }),
      variant: "danger",
      show: s === "IN_PROGRESS",
    },
    {
      label: "Approve",
      icon: <CheckIcon className="h-3.5 w-3.5" />,
      onClick: () => approveTask.mutate({ task_id: task.id }),
      variant: "success",
      show: s === "AWAITING_APPROVAL",
    },
    {
      label: "Approve Plan",
      icon: <DocumentCheckIcon className="h-3.5 w-3.5" />,
      onClick: () => approvePlan.mutate({ task_id: task.id }),
      variant: "success",
      show: s === "AWAITING_PLAN_APPROVAL",
    },
    {
      label: "Reject Plan",
      icon: <DocumentMinusIcon className="h-3.5 w-3.5" />,
      onClick: () => openModal("reject-plan"),
      variant: "danger",
      show: s === "AWAITING_PLAN_APPROVAL",
    },
    {
      label: "Delete Plan",
      icon: <TrashIcon className="h-3.5 w-3.5" />,
      onClick: () => deletePlan.mutate({ task_id: task.id }),
      variant: "secondary",
      show: s === "AWAITING_PLAN_APPROVAL",
    },
    {
      label: "Answer Question",
      icon: <ChatBubbleLeftIcon className="h-3.5 w-3.5" />,
      onClick: () => openModal("answer"),
      variant: "primary",
      show: s === "WAITING_INPUT",
    },
    {
      label: "Restart",
      icon: <ArrowPathIcon className="h-3.5 w-3.5" />,
      onClick: () => restartTask.mutate({ task_id: task.id }),
      variant: "primary",
      show: ["COMPLETED", "FAILED", "BLOCKED"].includes(s),
    },
    {
      label: "Skip",
      icon: <ForwardIcon className="h-3.5 w-3.5" />,
      onClick: () => skipTask.mutate({ task_id: task.id }),
      variant: "secondary",
      show: ["BLOCKED", "FAILED"].includes(s),
    },
    {
      label: "Reopen with Feedback",
      icon: <ArrowUturnLeftIcon className="h-3.5 w-3.5" />,
      onClick: () => openModal("reopen"),
      variant: "secondary",
      show: ["COMPLETED", "FAILED"].includes(s),
    },
    {
      label: "Delete",
      icon: <TrashIcon className="h-3.5 w-3.5" />,
      onClick: () => openModal("delete"),
      variant: "danger",
      show: s !== "IN_PROGRESS",
    },
  ];

  const visible = buttons.filter((b) => b.show);
  if (visible.length === 0) return null;

  const variantClasses: Record<string, string> = {
    success: "bg-emerald-600 hover:bg-emerald-500 text-white",
    danger: "bg-red-600/80 hover:bg-red-500 text-white",
    primary: "bg-indigo-600 hover:bg-indigo-500 text-white",
    secondary: "border border-gray-600 bg-gray-800 hover:bg-gray-700 text-gray-200",
  };

  const modalTitles: Record<string, string> = {
    "reject-plan": "Reject Plan",
    reopen: "Reopen with Feedback",
    answer: "Answer Agent Question",
    delete: "Delete Task",
  };

  return (
    <>
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase text-gray-500">Actions</h2>
        <div className="flex flex-wrap gap-2">
          {visible.map((b) => (
            <button
              key={b.label}
              onClick={b.onClick}
              disabled={isPending}
              className={`inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors disabled:opacity-50 ${variantClasses[b.variant]}`}
            >
              {b.icon}
              {b.label}
            </button>
          ))}
        </div>
      </section>

      <Modal open={modal !== null && modal !== "delete"} onClose={closeModal} title={modalTitles[modal ?? ""] ?? ""}>
        <div className="space-y-4">
          <textarea
            value={textInput}
            onChange={(e) => setTextInput(e.target.value)}
            placeholder={
              modal === "answer"
                ? "Type your response to the agent..."
                : "Provide feedback..."
            }
            rows={4}
            className="w-full rounded-md border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-200 placeholder-gray-500 focus:border-indigo-500 focus:outline-none"
            autoFocus
          />
          <div className="flex justify-end gap-2">
            <button
              onClick={closeModal}
              className="rounded-md border border-gray-600 bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
            >
              Cancel
            </button>
            <button
              onClick={handleSubmitModal}
              disabled={!textInput.trim() || isPending}
              className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              {isPending ? "Submitting..." : "Submit"}
            </button>
          </div>
        </div>
      </Modal>

      <Modal open={modal === "delete"} onClose={closeModal} title="Delete Task">
        <div className="space-y-4">
          <p className="text-sm text-gray-300">
            Are you sure you want to delete <strong>{task.title}</strong>? This cannot be undone.
          </p>
          <div className="flex justify-end gap-2">
            <button
              onClick={closeModal}
              className="rounded-md border border-gray-600 bg-gray-800 px-3 py-1.5 text-sm text-gray-300 hover:bg-gray-700"
            >
              Cancel
            </button>
            <button
              onClick={handleSubmitModal}
              disabled={isPending}
              className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-500 disabled:opacity-50"
            >
              {isPending ? "Deleting..." : "Delete"}
            </button>
          </div>
        </div>
      </Modal>
    </>
  );
}
