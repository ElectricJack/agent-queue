import { useCallback, useEffect, useMemo, useRef, useState, type ComponentProps } from "react";
import { createPortal } from "react-dom";
import type { ExtraProps } from "react-markdown";

import { useCommentReview, useDecideReview, useImportReviewEdits, useReopenReview, useReview, type ReviewAttachment } from "../../api/reviews";
import WithdrawReviewModal from "../../pages/reviews/WithdrawReviewModal";
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
import { ReviewAttachments } from "./ReviewAttachments";
import { downloadReviewMarkdown } from "./download";
import { useCommentPosition, type CommentReference } from "./useCommentPosition";

type ReviewRecord = {
  id: string;
  title: string;
  kind: string;
  state: string;
  current_revision: number;
  decider: string;
  withdrawn_by?: string | null;
  withdrawn_at?: number | null;
  withdrawn_via?: string | null;
  withdrawal_reason?: string | null;
};

type ReviewResponse = {
  review: ReviewRecord;
  revision: { revision: number; content: string; changes_note?: string | null };
  revisions: RevisionSummary[];
  vault_state: string;
  comments?: ReviewComment[] | null;
  diff?: { op: "equal" | "added" | "removed"; text: string }[] | null;
  attachments?: ReviewAttachment[];
  response_route: ResponseRoute;
  dependent_task_ids?: string[];
  gate?: { status: string } | null;
};

type CommentTarget = {
  anchor: Anchor;
  reference: CommentReference;
  returnFocus: HTMLElement;
  revision: number;
};

function SelectionCommentButton({ target, onOpen }: { target: CommentTarget; onOpen: () => void }) {
  const ref = useRef<HTMLButtonElement>(null);
  const style = useCommentPosition(target.reference, ref, { withinContainer: true });
  return createPortal(
    <button ref={ref} type="button" style={style}
      onPointerDown={(event) => event.preventDefault()}
      onClick={onOpen}
      className="z-40 rounded bg-indigo-600 px-2 py-1 text-xs font-medium text-white shadow hover:bg-indigo-500">
      Comment
    </button>, document.body,
  );
}

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
  const [selection, setSelection] = useState<CommentTarget | null>(null);
  const [commentTarget, setCommentTarget] = useState<CommentTarget | null>(null);
  const [revisedSinceOpen, setRevisedSinceOpen] = useState(false);
  const [closing, setClosing] = useState(false);
  const [reopenError, setReopenError] = useState<string | null>(null);
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
  const reopen = useReopenReview();

  useEffect(() => {
    if (response && selectedRevision == null) setSelectedRevision(response.review.current_revision);
  }, [response, selectedRevision]);

  const onEvent = useCallback((event: NotifyEvent) => {
    if (event.event_type === "review.revised" && event.review_id === reviewId) setRevisedSinceOpen(true);
  }, [reviewId]);
  useRawEventSubscription(onEvent);

  const content = response?.revision.content ?? "";
  const canDownload = !query.error && response?.review.id === reviewId
    && response.revision.revision === viewedRevision
    && typeof response.revision.content === "string";
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
    if (viewedRevision == null) return;
    // A cloned Range keeps measuring the quote after clicking/focusing the editor.
    const range = browserSelection.getRangeAt(0).cloneRange();
    const element = range.startContainer.nodeType === Node.ELEMENT_NODE
      ? range.startContainer as HTMLElement : range.startContainer.parentElement!;
    const context = element.closest<HTMLElement>("p, li, pre, blockquote, h2, h3") ?? element;
    const rect = () => typeof range.getBoundingClientRect === "function"
      ? range.getBoundingClientRect() : element.getBoundingClientRect();
    setSelection({
      anchor, revision: viewedRevision, returnFocus: container,
      reference: { container, element, rect, contextRect: () => context.getBoundingClientRect() },
    });
  }, [viewedRevision]);

  const submitComment = useCallback(async (target: CommentTarget, body: string) => {
    await comment.mutateAsync({
      review_id: reviewId,
      revision: target.revision,
      quote: target.anchor.quote,
      heading_path: target.anchor.heading_path,
      body,
    });
  }, [comment, reviewId]);

  const closeComment = useCallback(() => {
    setCommentTarget(null);
    setSelection(null);
  }, []);

  const openSectionComment = useCallback((anchor: Anchor, button: HTMLButtonElement) => {
    const container = bodyRef.current;
    const heading = button.closest<HTMLElement>("h2, h3");
    if (!container || !heading || viewedRevision == null) return;
    setCommentTarget({
      anchor, revision: viewedRevision, returnFocus: button,
      reference: {
        container, element: heading, rect: () => heading.getBoundingClientRect(),
        contextRect: () => {
          const rect = heading.getBoundingClientRect();
          let next = heading.nextElementSibling;
          if (heading.tagName === "H2") {
            while (next?.matches("h3")) next = next.nextElementSibling;
          }
          // Preserve the first paragraph too; a section is more than its title.
          const paragraph = next?.matches("p") ? next.getBoundingClientRect() : rect;
          return {
            left: Math.min(rect.left, paragraph.left), right: Math.max(rect.right, paragraph.right),
            top: rect.top, bottom: Math.max(rect.bottom, paragraph.bottom),
          };
        },
      },
    });
  }, [viewedRevision]);

  const submitDecision = useCallback(async (
    decision: ReviewDecision, note: string, responderClass: string,
  ) => {
    if (!response || viewedRevision == null) return;
    await decide.mutateAsync({
      review_id: response.review.id, revision: viewedRevision, decision, ...(note ? { note } : {}),
      ...(decision !== "approve" && responderClass ? { responder_class: responderClass } : {}),
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

  // Stable component identities keep the originating heading/control connected when opening.
  const headingComponents = useMemo(() => ({
    h2: ({ node, children, ...props }: ComponentProps<"h2"> & ExtraProps) => {
      void node;
      const heading = textFromChildren(children).trim();
      const path = headingPathAt(renderedBody, headingOffset(renderedBody, 2, heading));
      return (
        <h2 {...props} className="group flex flex-wrap scroll-mt-4 items-center gap-2">
          <span>{children}</span>
          <button
            type="button"
            onClick={(event) => openSectionComment({ quote: null, heading_path: path }, event.currentTarget)}
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
        <h3 {...props} className="group flex flex-wrap scroll-mt-4 items-center gap-2">
          <span>{children}</span>
          <button
            type="button"
            onClick={(event) => openSectionComment({ quote: null, heading_path: path }, event.currentTarget)}
            className="invisible rounded border border-gray-700 px-1.5 py-0.5 text-xs font-normal text-gray-400 group-hover:visible focus:visible hover:bg-gray-800"
          >
            Comment on this section
          </button>
        </h3>
      );
    },
  }), [renderedBody, openSectionComment]);

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

  return (
    <div className="@container/review flex h-full min-h-0 flex-col bg-gray-950 text-gray-100">
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
        downloadDisabled={!canDownload}
        onDownloadMarkdown={() => {
          if (canDownload) {
            downloadReviewMarkdown(
              response.revision.content, response.review.title, response.review.id,
              response.revision.revision,
            );
          }
        }}
      />
      <div className="border-b border-gray-800 p-4">
        <h1 className="mb-2 text-xl font-semibold">{title}</h1>
        <MetaCard data={parsed.data} onCompanionClick={() => {}} />
        {response.review.withdrawn_by && (
          <p className="mt-2 text-sm text-gray-400">
            Last withdrawn by {response.review.withdrawn_by} via {response.review.withdrawn_via ?? "unknown surface"}
            {response.review.withdrawn_at != null && ` on ${new Date(response.review.withdrawn_at * 1000).toLocaleString()}`}.
            {response.review.withdrawal_reason && ` Reason: ${response.review.withdrawal_reason}`}
          </p>
        )}
        {response.review.state === "withdrawn" && (
          <div className="mt-3 space-y-2 text-sm text-amber-200">
            <p>This review was withdrawn. Its approval gate is {response.gate?.status ?? "cancelled"}.
              Dependent work still requires approval.</p>
            <p>Dependent tasks: {(response.dependent_task_ids ?? []).length === 0 ? "none" :
              (response.dependent_task_ids ?? []).map((taskId, index) => (
                <span key={taskId}>{index > 0 && ", "}<a className="underline" href={`/tasks/${encodeURIComponent(taskId)}`}>{taskId}</a></span>
              ))}</p>
            <button type="button" disabled={reopen.isPending}
              className="rounded bg-indigo-700 px-3 py-1.5 text-sm text-white disabled:opacity-50"
              onClick={async () => {
                setReopenError(null);
                try {
                  const result = await reopen.mutateAsync({
                    review_id: response.review.id, revision: response.review.current_revision,
                  });
                  setSelectedRevision(result.revision);
                  setRevisedSinceOpen(false);
                  setShowDiff(false);
                } catch (error) {
                  setReopenError(error instanceof Error ? error.message : String(error));
                }
              }}>
              {reopen.isPending ? "Reopening…" : "Reopen review"}
            </button>
            {reopenError && <p role="alert" className="text-red-300">{reopenError}</p>}
          </div>
        )}
        {["in_review", "changes_requested", "rejected"].includes(response.review.state) && (
          <button type="button" onClick={() => setClosing(true)}
            className="mt-3 rounded border border-gray-700 px-3 py-1.5 text-sm text-gray-300">
            Close review
          </button>
        )}
      </div>
      {closing && <WithdrawReviewModal review={response.review} onClose={() => setClosing(false)}
        onWithdrawn={() => setClosing(false)} />}
      {showDiff && <DiffBlocks blocks={response.diff} />}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {toc.length > 0 && (
          <aside className="hidden w-48 shrink-0 overflow-y-auto border-r border-gray-800 p-3 @min-[800px]/review:block">
            <TocList
              toc={toc}
              activeId={null}
              onSelect={(id) => bodyRef.current?.querySelector(`#${CSS.escape(id)}`)?.scrollIntoView({ block: "start" })}
              tocRef={tocRef}
            />
          </aside>
        )}
        <div ref={bodyRef} tabIndex={-1} onMouseUp={selectCurrentText} className="relative min-w-0 flex-1 overflow-y-auto p-5" data-review-body>
          <MarkdownPreview source={renderedBody} headingComponents={headingComponents} />
          <ReviewAttachments
            reviewId={reviewId}
            revision={viewedRevision!}
            attachments={response.attachments ?? []}
            editable={response.review.state === "in_review" && viewedRevision === response.review.current_revision && !revisedSinceOpen}
          />
          {selection && !commentTarget && (
            <SelectionCommentButton target={selection} onOpen={() => setCommentTarget(selection)} />
          )}
          {commentTarget && (
            <CommentPopover
              anchor={commentTarget.anchor}
              reference={commentTarget.reference}
              returnFocus={commentTarget.returnFocus}
              onClose={closeComment}
              onSubmit={(body) => submitComment(commentTarget, body)}
            />
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
