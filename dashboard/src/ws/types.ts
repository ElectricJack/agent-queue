/**
 * Discriminated union of all notify.* event types from the backend EventBus.
 *
 * These mirror the Pydantic models in src/notifications/events.py.
 * Each event carries an event_type discriminator plus typed payload fields.
 */

// Reuse API types where possible
import type { Task, Agent } from "../api/hooks";

// --- Base fields present on every event ---

interface BaseEvent {
  _event_type: string;
  event_type: string;
  severity: string;
  category: string;
  project_id?: string | null;
}

// --- Task lifecycle ---

export interface TaskStartedEvent extends BaseEvent {
  event_type: "notify.task_started";
  task: Task;
  agent: Agent;
  workspace_path: string;
  workspace_name: string;
  is_reopened: boolean;
  task_description: string;
}

export interface TaskCompletedEvent extends BaseEvent {
  event_type: "notify.task_completed";
  task: Task;
  agent: Agent;
  summary: string;
  files_changed: string[];
  tokens_used: number;
}

export interface TaskFailedEvent extends BaseEvent {
  event_type: "notify.task_failed";
  task: Task;
  agent: Agent;
  error_label: string;
  error_detail: string;
  fix_suggestion: string;
  retry_count: number;
  max_retries: number;
}

export interface TaskBlockedEvent extends BaseEvent {
  event_type: "notify.task_blocked";
  task: Task;
  last_error: string;
}

export interface TaskStoppedEvent extends BaseEvent {
  event_type: "notify.task_stopped";
  task: Task;
}

// --- Interaction ---

export interface AgentQuestionEvent extends BaseEvent {
  event_type: "notify.agent_question";
  task: Task;
  agent: Agent;
  question: string;
}

export interface PlanAwaitingApprovalEvent extends BaseEvent {
  event_type: "notify.plan_awaiting_approval";
  task: Task;
  subtasks: Array<{ title: string; description?: string }>;
  plan_url: string;
  raw_content: string;
}

// --- VCS ---

export interface PRCreatedEvent extends BaseEvent {
  event_type: "notify.pr_created";
  task: Task;
  pr_url: string;
}

export interface MergeConflictEvent extends BaseEvent {
  event_type: "notify.merge_conflict";
  task: Task;
  branch: string;
  target_branch: string;
}

export interface PushFailedEvent extends BaseEvent {
  event_type: "notify.push_failed";
  task: Task;
  branch: string;
  error_detail: string;
}

// --- Budget & system ---

export interface BudgetWarningEvent extends BaseEvent {
  event_type: "notify.budget_warning";
  project_name: string;
  usage: number;
  limit: number;
  percentage: number;
}

export interface SystemOnlineEvent extends BaseEvent {
  event_type: "notify.system_online";
}

// --- Thread / streaming ---

export interface TaskThreadOpenEvent extends BaseEvent {
  event_type: "notify.task_thread_open";
  task_id: string;
  thread_name: string;
  initial_message: string;
}

export interface TaskMessageEvent extends BaseEvent {
  event_type: "notify.task_message";
  task_id: string;
  message: string;
  message_type: string;
}

export interface TaskThreadCloseEvent extends BaseEvent {
  event_type: "notify.task_thread_close";
  task_id: string;
  final_status: string;
  final_message: string;
}

// --- Generic ---

export interface TextNotifyEvent extends BaseEvent {
  event_type: "notify.text";
  message: string;
}

// --- Chain / stuck ---

export interface ChainStuckEvent extends BaseEvent {
  event_type: "notify.chain_stuck";
  blocked_task: Task;
  stuck_task_ids: string[];
  stuck_task_titles: string[];
}

export interface StuckDefinedTaskEvent extends BaseEvent {
  event_type: "notify.stuck_defined_task";
  task: Task;
  blocking_deps: Array<{ id: string; title: string; status: string }>;
  stuck_hours: number;
}

// --- Playbook lifecycle ---

export interface PlaybookRunStartedEvent extends BaseEvent {
  event_type: "notify.playbook_run_started";
  playbook_id: string;
  run_id: string;
  playbook_version: number;
  trigger_event_type: string;
  scope: string;
  started_at: number;
}

export interface PlaybookRunCompletedEvent extends BaseEvent {
  event_type: "notify.playbook_run_completed";
  playbook_id: string;
  run_id: string;
  final_context: string | null;
  tokens_used: number;
  duration_seconds: number;
  node_count: number;
}

export interface PlaybookRunFailedEvent extends BaseEvent {
  event_type: "notify.playbook_run_failed";
  playbook_id: string;
  run_id: string;
  failed_at_node: string;
  error: string;
  tokens_used: number;
  duration_seconds: number;
}

export interface PlaybookRunPausedEvent extends BaseEvent {
  event_type: "notify.playbook_run_paused";
  playbook_id: string;
  run_id: string;
  node_id: string;
  last_response: string;
  running_seconds: number;
  tokens_used: number;
  paused_at: number;
}

export interface PlaybookRunResumedEvent extends BaseEvent {
  event_type: "notify.playbook_run_resumed";
  playbook_id: string;
  run_id: string;
  node_id: string;
  decision: string;
}

export interface PlaybookRunTimedOutEvent extends BaseEvent {
  event_type: "notify.playbook_run_timed_out";
  playbook_id: string;
  run_id: string;
  node_id: string;
  timeout_seconds: number;
  waited_seconds: number;
  tokens_used: number;
  transitioned_to: string | null;
}

export interface PlaybookCompilationSucceededEvent extends BaseEvent {
  event_type: "notify.playbook_compilation_succeeded";
  playbook_id: string;
  source_path: string;
  version: number;
  source_hash: string;
  node_count: number;
  retries_used: number;
}

export interface PlaybookCompilationFailedEvent extends BaseEvent {
  event_type: "notify.playbook_compilation_failed";
  playbook_id: string;
  source_path: string;
  errors: string[];
  previous_version: number | null;
  source_hash: string;
  retries_used: number;
}

// --- Gate lifecycle ---

export interface GateCreatedEvent extends BaseEvent {
  event_type: "gate.created";
  gate_id: string;
  gate_type: string;
  title: string;
}

export interface GateResolvedEvent extends BaseEvent {
  event_type: "gate.resolved";
  gate_id: string;
  resolved_by: string;
  unblocked_task_ids?: string[];
}

export interface GateExpiredEvent extends BaseEvent {
  event_type: "gate.expired";
  gate_id: string;
}

// --- Message lifecycle ---

export interface MessageSentEvent extends BaseEvent {
  event_type: "message.sent";
  message_id: string;
  to_kind: string;
  to_id: string;
  thread_id?: string;
  reply_to_id?: string | null;
}

export interface MessageDeliveredEvent extends BaseEvent {
  event_type: "message.delivered";
  message_id: string;
  method?: string;
}

export interface MessageRepliedEvent extends BaseEvent {
  event_type: "message.replied";
  message_id: string;
  reply_id: string;
}

// --- Session lifecycle ---

export interface SessionStartedEvent extends BaseEvent {
  event_type: "session.started";
  session_id: string;
  name: string;
  task_id?: string;
}

export interface SessionExitedEvent extends BaseEvent {
  event_type: "session.exited";
  session_id: string;
  name: string;
  verdict: string;
}

export interface SessionAdoptedEvent extends BaseEvent {
  event_type: "session.adopted";
  session_id: string;
  name: string;
}

// --- Task blocked/unblocked (work-graph) ---

export interface TaskBlockedGraphEvent extends BaseEvent {
  event_type: "task.blocked";
  task_id: string;
  project_id: string;
  title: string;
  reason?: string;
}

export interface TaskUnblockedEvent extends BaseEvent {
  event_type: "task.unblocked";
  task_id: string;
  project_id: string;
  title: string;
  reason?: string;
}

// --- Command dispatch (Phase 5 follow-up) ---

/**
 * Emitted by the daemon's CommandHandler.execute after every dispatch
 * (success or failure). Powers the chat "supervisor is thinking…" bubble's
 * live tool-call chips and future observability surfaces.
 *
 * ``session_id`` / ``task_id`` / ``project_id`` come from the request's
 * server-derived scope; all three are null when the call arrived over the
 * trusted local (unauthenticated) path.
 */
export interface CommandInvokedEvent extends BaseEvent {
  event_type: "command.invoked";
  command: string;
  ok: boolean;
  duration_ms?: number;
  session_id?: string | null;
  task_id?: string | null;
  project_id?: string | null;
  args_summary?: string;
  error?: string | null;
}

// --- Union type ---

export type NotifyEvent =
  | TaskStartedEvent
  | TaskCompletedEvent
  | TaskFailedEvent
  | TaskBlockedEvent
  | TaskStoppedEvent
  | AgentQuestionEvent
  | PlanAwaitingApprovalEvent
  | PRCreatedEvent
  | MergeConflictEvent
  | PushFailedEvent
  | BudgetWarningEvent
  | SystemOnlineEvent
  | TaskThreadOpenEvent
  | TaskMessageEvent
  | TaskThreadCloseEvent
  | TextNotifyEvent
  | ChainStuckEvent
  | StuckDefinedTaskEvent
  | PlaybookRunStartedEvent
  | PlaybookRunCompletedEvent
  | PlaybookRunFailedEvent
  | PlaybookRunPausedEvent
  | PlaybookRunResumedEvent
  | PlaybookRunTimedOutEvent
  | PlaybookCompilationSucceededEvent
  | PlaybookCompilationFailedEvent
  | GateCreatedEvent
  | GateResolvedEvent
  | GateExpiredEvent
  | MessageSentEvent
  | MessageDeliveredEvent
  | MessageRepliedEvent
  | SessionStartedEvent
  | SessionExitedEvent
  | SessionAdoptedEvent
  | TaskBlockedGraphEvent
  | TaskUnblockedEvent
  | CommandInvokedEvent;
