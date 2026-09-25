import { useCallback, useEffect, useMemo, useRef, useState, type ComponentProps } from "react";
import type { ExtraProps } from "react-markdown";

import { useCommentReview, useDecideReview, useImportReviewEdits, useReview } from "../../api/reviews";
import MarkdownPreview from "../../components/MarkdownPreview";
import { useRawEventSubscription } from "../../ws/useEventStream";
import type { NotifyEvent } from "../../ws/types";
import type { PaneViewProps } from "../types";
import { MetaCard, TocList } from "../spec-doc-reader";
import { extractToc, parseFrontmatter, resolveTitle, stripLeadingH1 } from "../spec-doc-reader/docProcessing";
import { anchorFromSelection, headingPathAt, partitionComments, type Anchor } from "./anchoring";
import { CommentMargin, type ReviewComment } from "./CommentMargin";
import { CommentPopover } from "./CommentPopover";
import { DecisionBar, type ResponseRoute, type ReviewDecision } from "./DecisionBar";
import type { ReviewArgs } from "./manifest";
import { RevisionHeader, type RevisionSummary } from "./RevisionHeader";

type ReviewRecord = {
  id: string;
  title: string;
  kind: string;
  state: string;
  current_revision: number;
  decider: string;
};

type ReviewResponse = {
  review: ReviewRecord;
  revision: { revision: number; content: string; changes_note?: string | null };
  revisions: RevisionSummary[];
  vault_state: string;
  comments?: ReviewComment[] | null;
  diff?: { op: "equal" | "added" | "removed"; text: string }[] | null;
  response_route: ResponseRoute;
};

type SelectionPosition = { anchor: Anchor; left: number; top: number };

function textFromChildren(children: React.ReactNode): string {
  if (typeof children === "string" || typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(textFromChildren).join("");
  if (children && typeof children === "object" && "props" in children) {
    return textFromChildren((children as { props?: { children?: React.ReactNode } }).props?.children ?? "");
  }
  return "";
}

function headingOffset(markdown: string, depth: 2 | 3, heading: string): number {
  const escaped = heading.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`^#{${depth}}\\s+${escaped}\\s*#*\\s*$`, "m").exec(markdown);
  return match?.index ?? 0;
}

function DiffBlocks({ blocks }: { blocks: ReviewResponse["diff"] }) {
  if (!blocks || blocks.length === 0) return null;
  return (
    <section aria-label="Changes since previous" className="border-b border-gray-800 p-4">
      <h2 className="mb-2 text-sm font-semibold text-gray-200">Changes since previous</h2>
      <div className="space-y-2 text-sm">
        {blocks.map((block, index) => (
          <pre
            key={`${block.op}-${index}`}
            className={
              "overflow-x-auto whitespace-pre-wrap rounded p-2 " +
              (block.op === "added" ? "bg-emerald-950" : block.op === "removed" ? "bg-red-950" : "bg-gray-900")
            }
          >
            {block.text}
          </pre>
        ))}
      </div>
    </section>
  );
}

function ReviewPaneContent({ reviewId, setShortcuts }: { reviewId: string; setShortcuts?: PaneViewProps<ReviewArgs>["setShortcuts"] }) {
  const [selectedRevision, setSelectedRevision] = useState<number | null>(null);
  const [showDiff, setShowDiff] = useState(false);
  const [selection, setSelection] = useState<SelectionPosition | null>(null);
  const [commentAnchor, setCommentAnchor] = useState<Anchor | null>(null);
  const [revisedSinceOpen, setRevisedSinceOpen] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);
  const tocRef = useRef<HTMLDivElement>(null);
  const approveRef = useRef<HTMLButtonElement>(null);

  const requestedRevision = selectedRevision;
  const query = useReview(reviewId, {
    ...(requestedRevision != null ? { revision: requestedRevision } : {}),
    ...(showDiff && requestedRevision != null && requestedRevision > 1 ? { diffFrom: requestedRevision - 1 } : {}),
  });
  const response = query.data as unknown as ReviewResponse | undefined;
  const viewedRevision = selectedRevision ?? response?.review.current_revision;
  const comment = useCommentReview();
  const decide = useDecideReview();
  const importEdits = useImportReviewEdits();

  useEffect(() => {
    if (response && selectedRevision == null) setSelectedRevision(response.review.current_revision);
  }, [response, selectedRevision]);

  const onEvent = useCallback((event: NotifyEvent) => {
    if (event.event_type === "review.revised" && event.review_id === reviewId) setRevisedSinceOpen(true);
  }, [reviewId]);
  useRawEventSubscription(onEvent);

  const content = response?.revision.content ?? "";
  const parsed = useMemo(() => parseFrontmatter(content), [content]);
  const renderedBody = useMemo(() => stripLeadingH1(parsed.content), [parsed.content]);
  const toc = useMemo(() => extractToc(renderedBody), [renderedBody]);
  const title = response
    ? resolveTitle({ frontmatter: parsed.data, body: parsed.content, fallbackName: response.review.title })
    : "Review";
  const partitioned = useMemo(
    () => partitionComments(response?.comments ?? [], renderedBody, response?.review.current_revision ?? viewedRevision ?? 1),
    [response?.comments, renderedBody, response?.review.current_revision, viewedRevision],
  );

  const selectCurrentText = useCallback(() => {
    const container = bodyRef.current;
    const browserSelection = window.getSelection();
    if (!container || !browserSelection) return;
    const anchor = anchorFromSelection(browserSelection, container);
    if (!anchor) {
      setSelection(null);
      return;
    }
    const range = browserSelection.getRangeAt(0);
    const containerRect = container.getBoundingClientRect();
    const rangeRect = typeof range.getBoundingClientRect === "function"
      ? range.getBoundingClientRect()
      : containerRect;
    setSelection({
      anchor,
      left: Math.max(0, rangeRect.left - containerRect.left),
      top: Math.max(0, rangeRect.bottom - containerRect.top),
    });
  }, []);

  const submitComment = useCallback(async (anchor: Anchor, body: string) => {
    if (!response || viewedRevision == null) return;
    await comment.mutateAsync({
      review_id: response.review.id,
      revision: viewedRevision,
      quote: anchor.quote,
      heading_path: anchor.heading_path,
      body,
    });
  }, [comment, response, viewedRevision]);

  const submitDecision = useCallback(async (
    decision: ReviewDecision, note: string, responderClass: string, responderProfile: string,
  ) => {
    if (!response || viewedRevision == null) return;
    await decide.mutateAsync({
      review_id: response.review.id, revision: viewedRevision, decision, ...(note ? { note } : {}),
      ...(decision !== "approve" && responderClass ? { responder_class: responderClass } : {}),
      ...(decision !== "approve" && responderProfile ? { responder_profile: responderProfile } : {}),
    });
  }, [decide, response, viewedRevision]);

  useEffect(() => {
    if (!setShortcuts) return;
    setShortcuts([
      { key: "a", label: "Focus approve", onFire: () => approveRef.current?.focus() },
      { key: "c", label: "Comment on selection", onFire: selectCurrentText },
    ]);
    return () => setShortcuts([]);
  }, [setShortcuts, selectCurrentText]);

  if (query.isLoading || !response) {
    return <div className="p-5 text-sm text-gray-500">Loading review…</div>;
  }
  if (query.error) {
    return <div role="alert" className="p-5 text-sm text-red-300">Could not load this review.</div>;
  }

  const disabledReason = response.vault_state === "diverged"
    ? "edited outside the review"
    : revisedSinceOpen || viewedRevision! < response.review.current_revision
      ? "revised since you opened it — reload"
      : response.review.decider === "user_or_supervisor"
        ? "delegated to supervisor"
        : response.review.state !== "in_review"
          ? "this review is closed"
          : null;

  const headingComponents = {
    h2: ({ node, children, ...props }: ComponentProps<"h2"> & ExtraProps) => {
      void node;
      const heading = textFromChildren(children).trim();
      const path = headingPathAt(renderedBody, headingOffset(renderedBody, 2, heading));
      return (
        <h2 {...props} className="group flex scroll-mt-4 items-center gap-2">
          <span>{children}</span>
          <button
            type="button"
            onClick={() => setCommentAnchor({ quote: null, heading_path: path })}
            className="invisible rounded border border-gray-700 px-1.5 py-0.5 text-xs font-normal text-gray-400 group-hover:visible focus:visible hover:bg-gray-800"
          >
            Comment on this section
          </button>
        </h2>
      );
    },
    h3: ({ node, children, ...props }: ComponentProps<"h3"> & ExtraProps) => {
      void node;
      const heading = textFromChildren(children).trim();
      const path = headingPathAt(renderedBody, headingOffset(renderedBody, 3, heading));
      return (
        <h3 {...props} className="group flex scroll-mt-4 items-center gap-2">
          <span>{children}</span>
          <button
            type="button"
            onClick={() => setCommentAnchor({ quote: null, heading_path: path })}
            className="invisible rounded border border-gray-700 px-1.5 py-0.5 text-xs font-normal text-gray-400 group-hover:visible focus:visible hover:bg-gray-800"
          >
            Comment on this section
          </button>
        </h3>
      );
    },
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-gray-950 text-gray-100">
      {revisedSinceOpen && (
        <div className="border-b border-amber-900 bg-amber-950/50 px-4 py-2 text-sm text-amber-200">
          Revised since you opened it — reload
        </div>
      )}
      {response.vault_state === "diverged" && (
        <div className="flex flex-wrap items-center gap-3 border-b border-red-900 bg-red-950/40 px-4 py-2 text-sm text-red-200">
          <span>This file was edited outside the review</span>
          <button
            type="button"
            disabled={importEdits.isPending}
            onClick={() => void importEdits.mutateAsync({ review_id: response.review.id })}
            className="rounded border border-red-700 px-2 py-1 text-xs hover:bg-red-900 disabled:opacity-50"
          >
            Import my edits
          </button>
        </div>
      )}
      <RevisionHeader
        state={response.review.state}
        revisions={response.revisions}
        revision={viewedRevision!}
        onRevisionChange={(revision) => {
          setSelectedRevision(revision);
          setShowDiff(false);
          setRevisedSinceOpen(false);
        }}
        showDiff={showDiff}
        onShowDiffChange={setShowDiff}
      />
      <div className="border-b border-gray-800 p-4">
        <h1 className="mb-2 text-xl font-semibold">{title}</h1>
        <MetaCard data={parsed.data} onCompanionClick={() => {}} />
      </div>
      {showDiff && <DiffBlocks blocks={response.diff} />}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {toc.length > 0 && (
          <aside className="hidden w-48 shrink-0 overflow-y-auto border-r border-gray-800 p-3 min-[720px]:block">
            <TocList
              toc={toc}
              activeId={null}
              onSelect={(id) => bodyRef.current?.querySelector(`#${CSS.escape(id)}`)?.scrollIntoView({ block: "start" })}
              tocRef={tocRef}
            />
          </aside>
        )}
        <div ref={bodyRef} onMouseUp={selectCurrentText} className="relative min-w-0 flex-1 overflow-y-auto p-5" data-review-body>
          <MarkdownPreview source={renderedBody} headingComponents={headingComponents} />
          {selection && !commentAnchor && (
            <button
              type="button"
              style={{ left: selection.left, top: selection.top }}
              onClick={() => setCommentAnchor(selection.anchor)}
              className="absolute z-10 rounded bg-indigo-600 px-2 py-1 text-xs font-medium text-white shadow hover:bg-indigo-500"
            >
              Comment
            </button>
          )}
          {commentAnchor && (
            <div className="absolute z-20 left-4 top-4">
              <CommentPopover
                anchor={commentAnchor}
                onClose={() => {
                  setCommentAnchor(null);
                  setSelection(null);
                }}
                onSubmit={(body) => submitComment(commentAnchor, body)}
              />
            </div>
          )}
        </div>
        <CommentMargin anchored={partitioned.anchored} earlier={partitioned.earlier} />
      </div>
      <DecisionBar key={`${reviewId}:${viewedRevision}`} disabledReason={disabledReason} onDecide={submitDecision} pending={decide.isPending} approveButtonRef={approveRef} responseRoute={response.response_route} />
    </div>
  );
}

/** Full-width route surface and pane implementation share the same reader. */
export function ReviewPane({ reviewId }: { reviewId: string }) {
  return <ReviewPaneContent reviewId={reviewId} />;
}

export default function ReviewPaneView({ args, setShortcuts }: PaneViewProps<ReviewArgs>) {
  return <ReviewPaneContent reviewId={args.reviewId} setShortcuts={setShortcuts} />;
}
