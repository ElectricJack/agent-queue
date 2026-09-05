import { useEffect, useRef } from "react";
import { FieldError } from "./FieldError";
import { deriveProjectIdentity, projectIdError } from "./identity";
import { useFieldErrorProps, useWizard } from "./context";

/** Editable identity, with immediate client-side checks before the server repeats them. */
export function ProjectIdentityStep() {
  const { state, dispatch, projectIds } = useWizard();
  const nameProps = useFieldErrorProps("projectName");
  const idProps = useFieldErrorProps("projectId");
  const branchProps = useFieldErrorProps("defaultBranch");
  const immediateError = projectIdError(state.identity.projectId, projectIds);
  const derivedFor = useRef<string | null>(null);
  useEffect(() => {
    const derived = deriveProjectIdentity(state.source);
    const sourceKey = JSON.stringify(state.source);
    if (!derived || derivedFor.current === sourceKey) return;
    derivedFor.current = sourceKey;
    dispatch({ type: "update_identity", patch: {
      projectName: state.identity.projectName || derived.projectName,
      projectId: state.identity.projectId || derived.projectId,
    } });
  }, [state.source, state.identity.projectName, state.identity.projectId, dispatch]);
  return <div className="space-y-4">
    <p className="text-sm text-gray-400">Choose how this project appears in Agent Queue. The ID is used in its URL.</p>
    <label className="block text-sm text-gray-200">Display name
      <input {...nameProps} value={state.identity.projectName} onChange={(e) => dispatch({ type: "update_identity", patch: { projectName: e.target.value } })} className="mt-1 block w-full rounded border border-gray-600 bg-gray-800 px-3 py-2" />
    </label>
    <FieldError name="projectName" />
    <label className="block text-sm text-gray-200">Project ID
      <input {...idProps} value={state.identity.projectId} onChange={(e) => dispatch({ type: "update_identity", patch: { projectId: e.target.value } })} className="mt-1 block w-full rounded border border-gray-600 bg-gray-800 px-3 py-2" />
    </label>
    {immediateError && <p role="alert" className="text-sm text-red-300">{immediateError}</p>}
    <FieldError name="projectId" />
    <label className="block text-sm text-gray-200">Default branch
      <input {...branchProps} value={state.identity.defaultBranch} onChange={(e) => dispatch({ type: "update_identity", patch: { defaultBranch: e.target.value } })} className="mt-1 block w-full rounded border border-gray-600 bg-gray-800 px-3 py-2" />
    </label>
    <FieldError name="defaultBranch" />
  </div>;
}
