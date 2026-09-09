import type { BranchChoice, DiscardBranch } from "../api/branchDiscard";

interface Props {
  branches: DiscardBranch[];
  choice: BranchChoice;
  onChoose: (choice: BranchChoice) => void;
}

/**
 * Asks what should happen to the branches a delete is about to orphan.
 *
 * Shown only after the server refuses with `hierarchy.branch_discard_required`,
 * which is how a caller that said nothing learns there is something to say.
 * "Keep" is preselected: leaving a ref behind is recoverable, deleting one is
 * not.
 */
export default function BranchDiscardPrompt({ branches, choice, onChoose }: Props) {
  const one = branches.length === 1;
  return (
    <div className="space-y-2 rounded-md border border-gray-700 bg-gray-900/60 p-3">
      <p className="text-sm text-gray-300">
        {one ? "This task has work on a branch:" : `${branches.length} tasks have work on branches:`}
      </p>
      <ul className="space-y-0.5">
        {branches.map((b) => (
          <li key={b.task_id} className="font-mono text-xs text-gray-400">
            {b.branch}
          </li>
        ))}
      </ul>
      <label className="flex items-center gap-2 text-sm text-gray-300">
        <input
          type="radio"
          name="branch-choice"
          checked={choice === "keep"}
          onChange={() => onChoose("keep")}
        />
        Keep {one ? "the branch" : "them"} on the remote
      </label>
      <label className="flex items-center gap-2 text-sm text-gray-300">
        <input
          type="radio"
          name="branch-choice"
          checked={choice === "delete"}
          onChange={() => onChoose("delete")}
        />
        Delete {one ? "the branch" : "them"} and the work on {one ? "it" : "them"} too
      </label>
    </div>
  );
}
