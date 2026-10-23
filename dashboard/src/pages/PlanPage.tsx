import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { getPlan } from "../api/client";
import MarkdownPreview from "../components/MarkdownPreview";

/** Plan deep links render in the dashboard, using only JSON from the daemon. */
export default function PlanPage() {
  const { taskId = "" } = useParams();
  const plan = useQuery({
    queryKey: ["plan", taskId],
    queryFn: async ({ signal }) => {
      const result = await getPlan({ path: { task_id: taskId }, signal });
      return result.data!;
    },
    retry: false,
  });

  return (
    <main className="mx-auto max-w-4xl p-6">
      <h1 className="mb-4 text-xl font-semibold">Plan — {taskId}</h1>
      {plan.isPending ? <p role="status">Loading plan…</p> : plan.isError ? (
        <p role="alert">Unable to load plan: {plan.error.message}</p>
      ) : (
        <MarkdownPreview source={plan.data.content} />
      )}
    </main>
  );
}
