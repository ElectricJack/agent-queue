import { useWizard } from "./context";

/** A human-readable, no-surprises description of every durable onboarding action. */
export function ReviewProjectStep() {
  const { state } = useWizard();
  const { source, identity } = state;
  const actions: string[] = [];
  if (source.mode === "link") actions.push(`Link the existing repository at ${source.relativePath ?? "the selected path"}.`);
  if (source.mode === "github_clone") actions.push(`Clone the selected GitHub repository into the selected destination.`);
  if (source.mode === "init") {
    actions.push(`Create a new Git repository at ${source.directoryName || "the selected destination"}.`);
    if (source.createReadme) actions.push("Create README.md and make the initial commit.");
    if (source.createGithub) actions.push(`Create the ${source.githubVisibility} GitHub repository ${source.githubOwner ? `${source.githubOwner}/` : ""}${source.githubRepo || source.directoryName}.`);
  }
  actions.push(`Register project ${identity.projectName || identity.projectId || "(unnamed project)"} and its primary workspace.`);
  return <div className="space-y-4 text-sm">
    <p className="text-gray-400">Review the persistent actions before creating this project.</p>
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2"><dt className="text-gray-500">Project ID</dt><dd>{identity.projectId || "Not set"}</dd><dt className="text-gray-500">Branch</dt><dd>{identity.defaultBranch}</dd></dl>
    <ul className="space-y-2">{actions.map((action) => <li key={action} className="rounded border border-gray-700 p-3">{action}</li>)}</ul>
    {source.mode === "init" && source.createGithub && <div className="rounded border border-amber-400/60 bg-amber-400/10 p-3 text-amber-100"><strong>GitHub creation</strong> creates an external repository.{!source.createReadme && " No commit exists, so AQ will create the GitHub repository without pushing a branch."}</div>}
  </div>;
}
