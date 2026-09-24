import type { QueryClient } from "@tanstack/react-query";
import { taskAttachmentsQuery } from "../../api/taskAttachments";
import { taskCommentsQuery } from "../../api/taskComments";
import { taskSessionsQuery } from "../../api/taskSessions";
import { taskSubtasksQuery } from "../../api/taskSubtasks";

/**
 * Start the task pane's section reads alongside the task read. The sections
 * mount only once the task has loaded and each then asks for its own data,
 * so opening a task was two round trips back to back: the task, then its
 * sessions, checklist, comments and attachments. Reads already fresh in the
 * cache are not repeated.
 */
export function prefetchTaskPane(client: QueryClient, taskId: string): void {
  if (!taskId) return;
  void client.prefetchQuery(taskSessionsQuery(taskId));
  void client.prefetchQuery(taskSubtasksQuery(taskId));
  void client.prefetchQuery(taskCommentsQuery(taskId, 0));
  void client.prefetchQuery(taskAttachmentsQuery(taskId));
}
