export function reviewMarkdownFilename(title: string, reviewId: string, revision: number): string {
  const sanitize = (value: string) => Array.from(value
    .replace(/[\p{Cc}\p{Cf}<>:"/\\|?*]/gu, "-")
    .replace(/\s+/g, " ")
    .replace(/^[ .-]+|[ .-]+$/g, ""))
    // Keep even four-byte Unicode names below common filesystem byte limits.
    .slice(0, 50).join("").replace(/[ .-]+$/g, "");
  const name = sanitize(title) || sanitize(reviewId) || "review";
  return `${name}-rev-${revision}.md`;
}

export function downloadReviewMarkdown(
  content: string, title: string, reviewId: string, revision: number,
): void {
  const url = URL.createObjectURL(new Blob([content], { type: "text/markdown;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = reviewMarkdownFilename(title, reviewId, revision);
  try {
    document.body.appendChild(link);
    link.click();
  } finally {
    link.remove();
    // Let the browser start consuming the Blob before releasing it.
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}
