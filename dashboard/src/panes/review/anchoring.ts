export interface Anchor {
  quote: string | null;
  heading_path: string[];
}

const HEADING = /^(#{2,3})\s+(.+?)\s*#*\s*$/gm;

/** Return the h2/h3 section hierarchy containing a markdown source offset. */
export function headingPathAt(markdown: string, offset: number): string[] {
  let currentH2: string | null = null;
  let path: string[] = [];
  let match: RegExpExecArray | null;

  HEADING.lastIndex = 0;
  while ((match = HEADING.exec(markdown)) !== null) {
    if (match.index > offset) break;
    const level = match[1]!.length;
    const text = match[2]!.trim();
    if (level === 2) {
      currentH2 = text;
      path = [text];
    } else {
      path = currentH2 ? [currentH2, text] : [text];
    }
  }
  return path;
}

function parentElement(node: Node | null): Element | null {
  return node?.nodeType === Node.ELEMENT_NODE ? node as Element : node?.parentElement ?? null;
}

function headingPathForNode(node: Node, container: HTMLElement): string[] {
  const start = parentElement(node);
  if (!start) return [];
  const headings = Array.from(container.querySelectorAll<HTMLElement>("h2, h3"));
  const position = headings.findIndex((heading) => heading.contains(start));
  let index = position;
  if (index === -1) {
    for (let candidate = headings.length - 1; candidate >= 0; candidate -= 1) {
      const relation = headings[candidate]!.compareDocumentPosition(start);
      if (relation & Node.DOCUMENT_POSITION_FOLLOWING) {
        index = candidate;
        break;
      }
    }
  }
  if (index < 0) return [];

  const heading = headings[index]!;
  const text = heading.textContent?.replace("Comment on this section", "").trim() ?? "";
  if (heading.tagName === "H2") return text ? [text] : [];
  for (let before = index - 1; before >= 0; before -= 1) {
    const parent = headings[before]!;
    if (parent.tagName === "H2") {
      const parentText = parent.textContent?.replace("Comment on this section", "").trim() ?? "";
      return parentText && text ? [parentText, text] : text ? [text] : [];
    }
  }
  return text ? [text] : [];
}

/** Convert an in-document DOM selection into the stable comment anchor shape. */
export function anchorFromSelection(selection: Selection, container: HTMLElement): Anchor | null {
  if (selection.rangeCount === 0 || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  if (!container.contains(range.startContainer) || !container.contains(range.endContainer)) return null;
  const quote = selection.toString().trim();
  if (!quote) return null;
  return { quote, heading_path: headingPathForNode(range.startContainer, container) };
}

/** The displayed copy may contain a repeated phrase; comments attach to the first match. */
export function locateQuote(renderedText: string, quote: string): number | null {
  const index = renderedText.indexOf(quote);
  return index === -1 ? null : index;
}

type MaybeResolved = { resolved?: boolean; resolved_at?: unknown; resolved_by?: unknown };

/** Split comments which still attach to the displayed revision from historical comments. */
export function partitionComments<C extends { quote: string | null; revision: number }>(
  comments: C[],
  renderedText: string,
  currentRevision: number,
): { anchored: C[]; earlier: C[] } {
  const anchored: C[] = [];
  const earlier: C[] = [];
  for (const comment of comments) {
    const missingQuote = comment.quote !== null && locateQuote(renderedText, comment.quote) === null;
    const details = comment as C & MaybeResolved;
    const resolved = details.resolved === true || details.resolved_at != null || details.resolved_by != null;
    if (missingQuote || (comment.revision < currentRevision && resolved)) earlier.push(comment);
    else anchored.push(comment);
  }
  return { anchored, earlier };
}
