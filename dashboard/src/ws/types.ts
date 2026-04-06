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
  | StuckDefinedTaskEvent;
