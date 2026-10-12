import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  StopIcon,
  PauseIcon,
  PlayIcon,
  ArrowPathIcon,
  ForwardIcon,
  TrashIcon,
  ChatBubbleLeftIcon,
  ArrowUturnLeftIcon,
  QuestionMarkCircleIcon,
} from "@heroicons/react/24/outline";
import type { Task } from "../api/hooks";
import {
  useStopTask,
  usePauseTask,
  useResumeTask,
  useRestartTask,
  useSkipTask,
  useReopenWithFeedback,
  useProvideInput,
} from "../api/hooks";
import { sendChatMessage } from "../api/chat";
import Modal from "./Modal";
import DeleteTaskModal from "./DeleteTaskModal";
import TaskAgentTerminalButton from "./TaskAgentTerminalButton";
import { canFocusTerminal } from "./terminalFocus";
import { useTerminalLinkMode } from "./terminalLinks";
import { focusSessionHref } from "../pages/focus/routes";
import { workspaceHref } from "../shell/projectNavigation";

interface TaskActionsProps {
  task: Task;
  returnTo?: string;
  onDeleted?: () => void;
  onOpenTerminal?: () => void;
}

type ModalType = "reopen" | "answer" | "delete" | null;

/** The supervisor session the agents page can render a terminal for. */
const SUPERVISOR_SESSION = "supervisor-global";
const SUPERVISOR_THREAD = "dashboard:global";

export default function TaskActions({ task, returnTo, onDeleted, onOpenTerminal }: TaskActionsProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const terminalMode = useTerminalLinkMode();
  const [modal, setModal] = useState<ModalType>(null);
  const [textInput, setTextInput] = useState("");
  const stopTask = useStopTask();
  const pauseTask = usePauseTask();
  const resumeTask = useResumeTask();
  const restartTask = useRestartTask();
  const skipTask = useSkipTask();
  const reopenWithFeedback = useReopenWithFeedback();
  const provideInput = useProvideInput();

  // "Why is this blocked?" — ask the global supervisor in its own chat thread
  // (the same session the agents page renders a terminal for), then switch the
  // view to that terminal so the answer is watched where it is produced.
  const [asking, setAsking] = useState(false);
  const [askError, setAskError] = useState<Error | null>(null);
  const askSupervisor = async () => {
    setAsking(true);
    setAskError(null);
    try {
      await sendChatMessage("", `Why is task ${task.id} ("${task.title}") blocked?`, {
        sessionAddress: SUPERVISOR_SESSION,
        threadId: SUPERVISOR_THREAD,
      });
      // Focus routes watch the supervisor's session instead of attaching on /agents.
      if (terminalMode === "watch") navigate(focusSessionHref(SUPERVISOR_SESSION));
      else navigate(`/agents?agent=${SUPERVISOR_SESSION}`, { state: {
        agentSelection: "replace",
        ...(canFocusTerminal() ? { terminalFocus: SUPERVISOR_SESSION } : {}),
      } });
    } catch (err) {
      setAskError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      setAsking(false);
    }
  };

  const isPending =
    pauseTask.isPending || resumeTask.isPending ||
    stopTask.isPending ||
    restartTask.isPending ||
    skipTask.isPending ||
    reopenWithFeedback.isPending ||
    asking ||
    provideInput.isPending;

  const s = task.status?.toUpperCase() ?? "";

  const openModal = (type: ModalType) => {
    setTextInput("");
    setModal(type);
  };

  const closeModal = () => {
    setModal(null);
  };

  const handleSubmitModal = () => {
    if (modal === "reopen") {
      reopenWithFeedback.mutate({ task_id: task.id, feedback: textInput }, { onSuccess: closeModal });
    } else if (modal === "answer") {
      provideInput.mutate({ task_id: task.id, input: textInput }, { onSuccess: closeModal });

    }
  };

  const buttons: { label: string; icon: React.ReactNode; onClick: () => void; variant: string; show: boolean }[] = [
    {
      label: pauseTask.isPending ? "Pausing…" : "Pause",
      icon: <PauseIcon className="h-3.5 w-3.5" />,
      onClick: () => { resumeTask.reset(); pauseTask.mutate({ task_id: task.id }); },
      variant: "primary",
      show: ["DEFINED", "READY", "ASSIGNED", "IN_PROGRESS", "BLOCKED", "WAITING_INPUT"].includes(s),
    },
    {
      label: resumeTask.isPending ? "Resuming…" : "Resume",
      icon: <PlayIcon className="h-3.5 w-3.5" />,
      onClick: () => { pauseTask.reset(); resumeTask.mutate({ task_id: task.id }); },
      variant: "primary",
      show: s === "PAUSED",
    },
    {
      label: "Stop",
      icon: <StopIcon className="h-3.5 w-3.5" />,
      onClick: () => stopTask.mutate({ task_id: task.id }),
      variant: "danger",
      show: s === "IN_PROGRESS",
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
      label: asking ? "Asking…" : "Ask supervisor why",
      icon: <QuestionMarkCircleIcon className="h-3.5 w-3.5" />,
      onClick: () => { void askSupervisor(); },
      variant: "secondary",
      show: s === "BLOCKED",
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
      show: true,
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
    reopen: "Reopen with Feedback",
    answer: "Answer Agent Question",
    delete: "Delete Task",
  };

  return (
    <>
      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase text-gray-500">Actions</h2>
        <div className="flex flex-wrap gap-2">
          {task.assigned_agent && <TaskAgentTerminalButton task={task} onOpen={onOpenTerminal} />}
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
        {(pauseTask.error || resumeTask.error || askError) && <p role="alert" className="mt-2 text-sm text-red-300">
          {(pauseTask.error || resumeTask.error || askError)?.message}
        </p>}
      </section>

      {modal === "delete" && <DeleteTaskModal task={task} onClose={closeModal} onDeleted={() => {
        onDeleted?.();
        const destination = returnTo ?? (location.state as { from?: string } | null)?.from ?? workspaceHref(task.project_id, "tasks-knowledge");
        if (destination !== location.pathname + location.search) navigate(destination);
      }} />}
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


    </>
  );
}
