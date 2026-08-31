import { useId, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PencilIcon } from "@heroicons/react/24/outline";
import { taskSet } from "../api/client";

export default function TaskDescription({ task }: { task: { id: string; description?: string } }) {
  const headingId = useId();
  const inputId = useId();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [baseline, setBaseline] = useState("");
  const queryClient = useQueryClient();
  const save = useMutation({
    mutationFn: async () => (await taskSet({
      body: { task_id: task.id, description: draft, expected_description: baseline },
      throwOnError: true,
    })).data,
    onSuccess: () => {
      setEditing(false);
      void queryClient.invalidateQueries({ queryKey: ["task", task.id] });
      void queryClient.invalidateQueries({ queryKey: ["tasks"] });
      void queryClient.invalidateQueries({ queryKey: ["projectGraph"] });
    },
    onError: () => { void queryClient.invalidateQueries({ queryKey: ["task", task.id] }); },
  });

  return (
    <section aria-labelledby={headingId} className="space-y-2">
      <div className="flex items-center justify-between gap-3">
        <h2 id={headingId} className="text-sm font-semibold uppercase text-gray-500">Description</h2>
        {!editing && <button type="button" aria-label="Edit description" onClick={() => {
          setBaseline(task.description ?? "");
          setDraft(task.description ?? "");
          save.reset();
          setEditing(true);
        }} className="inline-flex items-center gap-1 text-xs text-indigo-400 hover:text-indigo-300">
          <PencilIcon className="h-3.5 w-3.5" /> Edit
        </button>}
      </div>
      {editing ? <form className="space-y-2" onSubmit={(event) => { event.preventDefault(); if (!save.isPending) save.mutate(); }}>
        <label htmlFor={inputId} className="sr-only">Description</label>
        <textarea id={inputId} value={draft} onChange={(event) => setDraft(event.target.value)}
          disabled={save.isPending} rows={6}
          className="w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-200 focus:border-indigo-500 focus:outline-none" />
        {save.isError && <div role="alert" className="space-y-1 text-sm text-red-300">
          <p>Could not save description. {save.error.message}</p>
          <p>Your draft is preserved. Copy it before cancelling to reload the latest description.</p>
        </div>}
        <div className="flex justify-end gap-2">
          <button type="button" aria-label="Cancel description edit" disabled={save.isPending}
            onClick={() => { setEditing(false); save.reset(); }}
            className="rounded border border-gray-700 px-3 py-1.5 text-sm text-gray-300 disabled:opacity-50">Cancel</button>
          <button type="submit" disabled={save.isPending || draft === baseline}
            className="rounded bg-indigo-600 px-3 py-1.5 text-sm text-white disabled:opacity-50">
            {save.isPending ? "Saving description…" : "Save description"}
          </button>
        </div>
      </form> : task.description ? <div className="whitespace-pre-wrap break-words rounded-lg border border-gray-800 bg-gray-900 p-3 text-sm text-gray-300">
        {task.description}
      </div> : <p className="text-sm text-gray-500">No description.</p>}
    </section>
  );
}
