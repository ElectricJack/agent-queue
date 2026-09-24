/**
 * What the list row or graph card a task was opened from already showed:
 * enough for the task pane's header while its full read is in flight, so a
 * click answers at once instead of with a blank "Loading…". Module memory,
 * bounded; the full read replaces it the moment it lands.
 */
export interface TaskPreview {
  id: string;
  title?: string | null;
  status?: string | null;
  priority?: number | null;
  project_id?: string | null;
}

const previews = new Map<string, TaskPreview>();
const LIMIT = 50;

export function rememberTaskPreview(task: { id: string } & Partial<Record<keyof TaskPreview, unknown>>): void {
  const text = (value: unknown) => (typeof value === "string" && value ? value : null);
  const preview: TaskPreview = {
    id: task.id,
    title: text(task.title),
    status: text(task.status),
    priority: typeof task.priority === "number" ? task.priority : null,
    project_id: text(task.project_id),
  };
  if (!preview.title && !preview.status) return;
  previews.delete(task.id);
  previews.set(task.id, preview);
  if (previews.size > LIMIT) {
    const oldest = previews.keys().next().value;
    if (oldest !== undefined) previews.delete(oldest);
  }
}

export function taskPreview(taskId: string): TaskPreview | null {
  return previews.get(taskId) ?? null;
}
