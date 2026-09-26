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
import { createContext, useContext, type ComponentProps } from "react";
import ReactMarkdown, { type Components, type ExtraProps } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSlug from "rehype-slug";

export interface MarkdownPreviewProps {
  source: string;
  className?: string;
  /** Documentation URLs keyed by inline command name. */
  inlineCodeLinks?: ReadonlyMap<string, string>;
  /** Optional review/document controls rendered inside h2 and h3 elements. */
  headingComponents?: Pick<Components, "h2" | "h3">;
}

// react-markdown's code component receives the same element shape for inline
// code and fenced blocks. Keep a tiny render-only context so the override can
// link only inline code while emitting fenced blocks exactly as before.
const InlineCodeContext = createContext(true);

function commandCodeComponents(inlineCodeLinks: ReadonlyMap<string, string>): Components {
  function Pre({ children, ...props }: ComponentProps<"pre"> & ExtraProps) {
    delete props.node;
    return (
      <InlineCodeContext.Provider value={false}>
        <pre {...props}>{children}</pre>
      </InlineCodeContext.Provider>
    );
  }

  function Code({ children, ...props }: ComponentProps<"code"> & ExtraProps) {
    delete props.node;
    const docsUrl = useContext(InlineCodeContext)
      ? inlineCodeLinks.get(String(children))
      : undefined;
    const code = <code {...props}>{children}</code>;

    return docsUrl ? (
      <a href={docsUrl} target="_blank" rel="noreferrer">
        {code}
      </a>
    ) : code;
  }

  return {
    pre: Pre,
    code: Code,
  };
}

export default function MarkdownPreview({
  source,
  className,
  inlineCodeLinks,
  headingComponents,
}: MarkdownPreviewProps) {
  const components = {
    ...(inlineCodeLinks ? commandCodeComponents(inlineCodeLinks) : {}),
    ...(headingComponents ?? {}),
  };
  return (
    <div
      className={
        "prose prose-invert max-w-none prose-pre:bg-black/40 prose-code:text-indigo-300 " +
        (className ?? "")
      }
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSlug]}
        components={Object.keys(components).length > 0 ? components : undefined}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
}
