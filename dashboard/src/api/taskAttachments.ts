import { queryOptions } from "@tanstack/react-query";
import { listAttachmentsApiTasksTaskIdAttachmentsGet, type TaskAttachmentsResponse } from "./client";

export const taskAttachmentsQuery = (taskId: string) => queryOptions({
  queryKey: ["task-attachments", taskId],
  queryFn: async () => (
    await listAttachmentsApiTasksTaskIdAttachmentsGet({
      path: { task_id: taskId },
      throwOnError: true,
    })
  ).data as TaskAttachmentsResponse,
});
