import { unified } from "unified";
import remarkParse from "remark-parse";
import remarkGfm from "remark-gfm";
import { visit } from "unist-util-visit";
import { toString as mdastToString } from "mdast-util-to-string";
import GithubSlugger from "github-slugger";
import matter from "gray-matter";
import type { Root, Heading } from "mdast";

export interface TocEntry {
  id: string;
  text: string;
  depth: 2 | 3;
}

/**
 * Parse `markdownSource` and return TOC entries for ## and ### headings
 * only. Slugs every heading (all depths) in document order through one
 * github-slugger instance so dedup counters match what rehype-slug
 * assigns the same headings during MarkdownPreview's render — react-markdown
 * doesn't expose its internal AST to the host component, so this runs its
 * own remark-parse pass over the same markdown source string. Parity is
 * achieved by both sides slugging every heading (all depths) with the same
 * github-slugger package in document order, not by AST sharing.
 */
export function extractToc(markdownSource: string): TocEntry[] {
  const tree = unified().use(remarkParse).use(remarkGfm).parse(markdownSource) as Root;
  const slugger = new GithubSlugger();
  const entries: TocEntry[] = [];

  visit(tree, "heading", (node: Heading) => {
    const text = mdastToString(node);
    const id = slugger.slug(text);
    if (node.depth === 2 || node.depth === 3) {
      entries.push({ id, text, depth: node.depth });
    }
  });

  return entries;
}

const BOLD_LABEL_LINE = /^\*\*([^*]+):\*\*\s*(.+)$/;

/**
 * Split frontmatter from body. Prefers a fenced ---/--- YAML block
 * (gray-matter). When none is present, falls back to a heuristic scan of
 * the preamble (everything before the first `##` heading) for
 * `**Label:** value` lines — this repo's own specs use that convention
 * instead of fenced YAML.
 */
function normalizeYamlValue(value: unknown): unknown {
  // js-yaml (via gray-matter) auto-parses bare dates (e.g. `date: 2026-08-22`)
  // into JS Date objects. Convert back to a plain ISO string so the meta
  // card renders "2026-08-22" instead of a Date's default toString().
  if (value instanceof Date) {
    const iso = value.toISOString();
    return iso.endsWith("T00:00:00.000Z") ? iso.slice(0, 10) : iso;
  }
  return value;
}

export function parseFrontmatter(raw: string): {
  data: Record<string, unknown> | null;
  content: string;
} {
  const { data, content } = matter(raw);
  if (data && Object.keys(data).length > 0) {
    const normalized = Object.fromEntries(
      Object.entries(data).map(([k, v]) => [k, normalizeYamlValue(v)]),
    );
    return { data: normalized, content };
  }

  const firstH2 = content.search(/^##\s/m);
  const preamble = firstH2 === -1 ? content : content.slice(0, firstH2);
  const fallback: Record<string, string> = {};
  for (const line of preamble.split("\n")) {
    const m = BOLD_LABEL_LINE.exec(line.trim());
    if (m && m[1] !== undefined && m[2] !== undefined) {
      fallback[m[1].trim()] = m[2].trim();
    }
  }
  return { data: Object.keys(fallback).length > 0 ? fallback : null, content };
}

const LEADING_H1 = /^\s*#[ \t]+.+?\n+/;

/**
 * Strip a single leading `#` (h1) heading from `content`, if present as the
 * very first thing in the document. The resolved title (§5.4) already
 * renders above the meta card, so re-rendering the same h1 again inside
 * the markdown body would duplicate it visually and in the DOM. TOC
 * extraction deliberately does NOT use this — `extractToc` needs the
 * unstripped source so its slugger consumes the h1 in document order for
 * dedup-counter parity with `rehype-slug` (see `extractToc`'s doc
 * comment); this helper is applied only to the body handed to
 * `MarkdownPreview`.
 */
export function stripLeadingH1(content: string): string {
  return content.replace(LEADING_H1, "");
}

function humanize(nameOrPath: string): string {
  const base = nameOrPath.split("/").pop() ?? nameOrPath;
  const noExt = base.replace(/\.(md|markdown)$/i, "");
  return noExt.replace(/[-_]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * Title resolution order: frontmatter `title`, then `name`, then the
 * body's first h1, then a humanized filename/URL-segment fallback.
 */
export function resolveTitle(opts: {
  frontmatter: Record<string, unknown> | null;
  body: string;
  fallbackName: string;
}): string {
  const fm = opts.frontmatter;
  if (fm) {
    if (typeof fm.title === "string" && fm.title.trim()) return fm.title.trim();
    if (typeof fm.name === "string" && fm.name.trim()) return fm.name.trim();
  }
  const h1 = /^#\s+(.+)$/m.exec(opts.body);
  if (h1 && h1[1] !== undefined) return h1[1].trim();
  return humanize(opts.fallbackName);
}
