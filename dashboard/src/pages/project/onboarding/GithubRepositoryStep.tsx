import { useEffect, useId, useState } from "react";
import { githubAuthStatus, searchGithub } from "./githubClient";
import { githubRepositoryDisplay } from "./githubUrl";
import { useWizard } from "./context";
import type { GithubRepositoryRef } from "./state";

function setupGuidance(installed?: boolean, message?: string | null) {
  if (!installed) return "GitHub CLI is not installed on the daemon host. Install gh and sign in to search repositories.";
  return message || "Sign in to GitHub on the daemon host with gh auth login to search repositories.";
}

export function GithubRepositoryStep() {
  const { state, dispatch, roots } = useWizard();
  const source = state.source;
  const isGithubClone = source.mode === "github_clone";
  const [authMessage, setAuthMessage] = useState<string | null>(null);
  const [repositories, setRepositories] = useState<GithubRepositoryRef[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const uid = useId();

  useEffect(() => {
    if (!isGithubClone) return;
    let active = true;
    void githubAuthStatus().then(
      (status) => {
        if (active && (!status.installed || !status.authenticated)) {
          setAuthMessage(setupGuidance(status.installed, status.message));
        }
      },
      () => active && setAuthMessage("GitHub setup could not be checked. You can still paste a repository URL for the server to validate."),
    );
    return () => { active = false; };
  }, [isGithubClone]);

  if (!isGithubClone) {
    return <p className="text-sm text-gray-400">Repository selection for this source is provided by its source-specific step.</p>;
  }

  const availableRoots = roots.status === "ready" ? roots.roots.filter((root) => root.writable) : [];
  const selected = source.githubRepository;
  const pasted = githubRepositoryDisplay(source.githubUrl);
  const search = async (append = false) => {
    const query = searchQuery.trim();
    if (!query) return;
    setSearching(true);
    setSearchError(null);
    try {
      const page = await searchGithub(query, append ? nextCursor : null);
      const values = page.repositories.map((repository) => ({
        owner: repository.owner,
        name: repository.name,
        cloneUrl: repository.clone_url_https,
        defaultBranch: repository.default_branch ?? null,
        visibility: repository.visibility === "public" ? "public" : "private",
      } satisfies GithubRepositoryRef));
      setRepositories((current) => append ? [...current, ...values] : values);
      setNextCursor(page.nextCursor);
    } catch {
      setSearchError("Could not search GitHub repositories. Check GitHub setup and try again.");
    } finally {
      setSearching(false);
    }
  };
  const selectRepository = (repository: GithubRepositoryRef) => {
    dispatch({ type: "update_source", mode: "github_clone", patch: {
      githubRepository: repository,
      githubUrl: "",
      directoryName: repository.name,
      directoryNameAuto: true,
    } });
  };
  const updatePastedUrl = (value: string) => {
    const display = githubRepositoryDisplay(value);
    dispatch({ type: "update_source", mode: "github_clone", patch: {
      githubUrl: value,
      githubRepository: null,
      directoryName: source.directoryNameAuto ? display?.name || "" : source.directoryName,
    } });
  };

  return (
    <div className="space-y-5">
      {authMessage && <p role="status" className="rounded border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-100">{authMessage}</p>}
      <div className="space-y-2">
        <label htmlFor={`${uid}-search`} className="block text-sm font-medium text-gray-200">Search GitHub repositories</label>
        <div className="flex gap-2">
          <input id={`${uid}-search`} value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="Search repositories" className="min-w-0 flex-1 rounded border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-100" />
          <button type="button" onClick={() => void search()} disabled={!searchQuery.trim() || searching} className="rounded border border-gray-600 px-3 py-2 text-sm text-gray-200 disabled:opacity-50">Search</button>
        </div>
        {searchError && <p role="alert" className="text-sm text-red-300">{searchError}</p>}
        {repositories.length > 0 && <ul aria-label="GitHub search results" className="divide-y divide-gray-700 rounded border border-gray-700">
          {repositories.map((repository) => <li key={`${repository.owner}/${repository.name}`}>
            <button type="button" onClick={() => selectRepository(repository)} className="w-full px-3 py-2 text-left text-sm hover:bg-gray-800">
              <span className="font-medium text-gray-100">{repository.owner}/{repository.name}</span>
              {repository.visibility && <span className="ml-2 text-xs text-gray-400">{repository.visibility}</span>}
            </button>
          </li>)}
        </ul>}
        {nextCursor && <button type="button" onClick={() => void search(true)} disabled={searching} className="text-sm text-indigo-300 underline disabled:opacity-50">Load more repositories</button>}
      </div>
      <div>
        <label htmlFor={`${uid}-url`} className="block text-sm font-medium text-gray-200">Or paste a GitHub repository URL</label>
        <input id={`${uid}-url`} value={source.githubUrl} onChange={(event) => updatePastedUrl(event.target.value)} placeholder="https://github.com/owner/repository" className="mt-1 w-full rounded border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-100" />
        {pasted && <p className="mt-1 text-xs text-gray-400">Will use GitHub repository {pasted.owner}/{pasted.name}. The original URL will be validated by the server.</p>}
        {selected && <p className="mt-1 text-xs text-gray-400">Selected repository: {selected.owner}/{selected.name}</p>}
      </div>
      <div>
        <label htmlFor={`${uid}-root`} className="block text-sm font-medium text-gray-200">Destination root</label>
        <select id={`${uid}-root`} value={source.rootId ?? ""} onChange={(event) => dispatch({ type: "update_source", mode: "github_clone", patch: { rootId: event.target.value } })} className="mt-1 w-full rounded border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-100">
          <option value="" disabled>Choose a writable project root</option>
          {availableRoots.map((root) => <option key={root.id} value={root.id}>{root.label} — {root.displayPath}</option>)}
        </select>
      </div>
      <div>
        <label htmlFor={`${uid}-directory`} className="block text-sm font-medium text-gray-200">Destination directory</label>
        <input id={`${uid}-directory`} value={source.directoryName} onChange={(event) => dispatch({ type: "update_source", mode: "github_clone", patch: { directoryName: event.target.value, directoryNameAuto: false } })} className="mt-1 w-full rounded border border-gray-600 bg-gray-800 px-3 py-2 text-sm text-gray-100" />
      </div>
    </div>
  );
}
