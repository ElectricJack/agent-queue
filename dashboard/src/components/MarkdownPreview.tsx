/**
 * MarkdownPreview — the dashboard's single canonical markdown renderer.
 *
 * Reused by:
 *   • Phase 5 (this) — task worktree file content when the file is *.md.
 *   • Phase 3 — spec preview in the supervisor chat page.
 *   • Phase 6 — playbook / profile preview in Settings.
 *
 * Uses remark-gfm because our vault markdown (specs, playbooks, profiles)
 * routinely relies on GitHub-flavored tables, task lists, and strikethrough.
 */
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export interface MarkdownPreviewProps {
  source: string;
  className?: string;
}

export default function MarkdownPreview({ source, className }: MarkdownPreviewProps) {
  return (
    <div
      className={
        "prose prose-invert max-w-none prose-pre:bg-black/40 prose-code:text-indigo-300 " +
        (className ?? "")
      }
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{source}</ReactMarkdown>
    </div>
  );
}
