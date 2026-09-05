import { useEffect, useId, useState } from "react";
import { githubAuthStatus, githubOwners } from "./githubClient";
import { useWizard } from "./context";

export function InitOptionsStep() {
  const { state, dispatch } = useWizard();
  const source = state.source;
  const [owners, setOwners] = useState<{ login: string; name?: string | null }[]>([]);
  const [setupMessage, setSetupMessage] = useState<string | null>(null);
  const uid = useId();
  const initSource = source.mode === "init" ? source : null;
  const createGithub = initSource?.createGithub ?? false;
  const githubOwner = initSource?.githubOwner ?? null;

  useEffect(() => {
    if (!initSource || !createGithub) return;
    let active = true;
    void githubAuthStatus().then(async (status) => {
      if (!active) return;
      if (!status.installed || !status.authenticated) {
        setSetupMessage(!status.installed ? "Install gh and sign in on the daemon host before creating a GitHub repository." : "Sign in with gh auth login on the daemon host before creating a GitHub repository.");
        return;
      }
      try {
        const values = await githubOwners();
        if (!active) return;
        setOwners(values);
        if (!githubOwner && values[0]) dispatch({ type: "update_source", mode: "init", patch: { githubOwner: values[0].login } });
      } catch {
        if (active) setSetupMessage("GitHub owners could not be loaded. Check GitHub setup and try again.");
      }
    });
    return () => { active = false; };
  }, [initSource, createGithub, githubOwner, dispatch]);

  if (!initSource) return <p className="text-sm text-gray-400">No additional options are needed for this source.</p>;
  return (
    <div className="space-y-5">
      <label className="flex items-center gap-2 text-sm text-gray-200"><input type="checkbox" checked={initSource.createReadme} onChange={(event) => dispatch({ type: "update_source", mode: "init", patch: { createReadme: event.target.checked } })} /> Create initial README and commit</label>
      <label className="flex items-center gap-2 text-sm text-gray-200"><input type="checkbox" checked={initSource.createGithub} onChange={(event) => dispatch({ type: "update_source", mode: "init", patch: { createGithub: event.target.checked } })} /> Create GitHub repository</label>
      {initSource.createGithub && <div className="space-y-4 rounded border border-gray-700 p-4">
        {setupMessage && <p role="status" className="text-sm text-amber-200">{setupMessage}</p>}
        <div><label htmlFor={`${uid}-owner`} className="block text-sm font-medium text-gray-200">GitHub owner</label><select id={`${uid}-owner`} value={initSource.githubOwner ?? ""} onChange={(event) => dispatch({ type: "update_source", mode: "init", patch: { githubOwner: event.target.value } })} className="mt-1 w-full rounded border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-100"><option value="" disabled>Choose an owner</option>{owners.map((owner) => <option key={owner.login} value={owner.login}>{owner.name ? `${owner.name} (${owner.login})` : owner.login}</option>)}</select></div>
        <div><label htmlFor={`${uid}-repo`} className="block text-sm font-medium text-gray-200">GitHub repository name</label><input id={`${uid}-repo`} value={initSource.githubRepo} onChange={(event) => dispatch({ type: "update_source", mode: "init", patch: { githubRepo: event.target.value } })} className="mt-1 w-full rounded border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-100" /></div>
        <fieldset><legend className="text-sm font-medium text-gray-200">Visibility</legend><label className="mr-4 text-sm text-gray-200"><input type="radio" name={`${uid}-visibility`} checked={initSource.githubVisibility === "private"} onChange={() => dispatch({ type: "update_source", mode: "init", patch: { githubVisibility: "private" } })} /> Private</label><label className="text-sm text-gray-200"><input type="radio" name={`${uid}-visibility`} checked={initSource.githubVisibility === "public"} onChange={() => dispatch({ type: "update_source", mode: "init", patch: { githubVisibility: "public" } })} /> Public</label></fieldset>
      </div>}
    </div>
  );
}
