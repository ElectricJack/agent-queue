import { useRef, useState } from "react";

import { useAttachReviewImage, type ReviewAttachment } from "../../api/reviews";

const MAX_BYTES = 10 * 1024 * 1024;
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);

function imageBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] ?? "");
    reader.onerror = () => reject(new Error("Could not read image"));
    reader.readAsDataURL(file);
  });
}

export function ReviewAttachments({
  reviewId, revision, attachments, editable,
}: {
  reviewId: string;
  revision: number;
  attachments: ReviewAttachment[];
  editable: boolean;
}) {
  const attach = useAttachReviewImage();
  const fileInput = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [caption, setCaption] = useState("");
  const [viewId, setViewId] = useState("");
  const [candidateId, setCandidateId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const comparison = attachments.length > 0 && attachments.every((attachment) =>
    ["before", "after"].includes(attachment.candidate_id.toLowerCase()));
  const ordered = comparison ? [...attachments].sort((a, b) =>
    a.view_id.localeCompare(b.view_id)
    || Number(a.candidate_id.toLowerCase() === "after")
      - Number(b.candidate_id.toLowerCase() === "after")) : attachments;

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!file) return;
    if (file.size > MAX_BYTES || !IMAGE_TYPES.has(file.type)) {
      setError("Choose a PNG, JPEG, GIF, or WebP image up to 10 MiB.");
      return;
    }
    try {
      setError(null);
      await attach.mutateAsync({
        review_id: reviewId, revision,
        data_base64: await imageBase64(file), content_type: file.type,
        caption, view_id: viewId, candidate_id: candidateId,
      });
      setFile(null);
      if (fileInput.current) fileInput.current.value = "";
      setCaption("");
      setViewId("");
      setCandidateId("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not attach image.");
    }
  }

  return (
    <section aria-label="Review screenshots" className="border-b border-gray-800 p-4">
      <h2 className="mb-2 text-sm font-semibold text-gray-200">{comparison ? "Before and after" : `Screenshots · revision ${revision}`}</h2>
      {attachments.length === 0 && <p className="text-sm text-gray-500">No screenshots attached.</p>}
      <div className={comparison ? "grid grid-cols-2 gap-3" : "grid gap-3 sm:grid-cols-2 lg:grid-cols-3"}>
        {ordered.map((attachment) => (
          <figure key={attachment.id} className="rounded border border-gray-800 bg-gray-900 p-2">
            <a href={attachment.url} target="_blank" rel="noreferrer" aria-label={`Open ${attachment.caption}`}>
              <img src={attachment.url} alt={attachment.caption} className="max-h-64 w-full object-contain" />
            </a>
            <figcaption className="mt-2 text-xs text-gray-300">
              <strong>{attachment.caption}</strong>
              <span className="block text-gray-400">{comparison ? attachment.view_id : `View ${attachment.view_id} · Candidate ${attachment.candidate_id}`}</span>
              {!comparison && <span className="block break-all font-mono text-gray-500">SHA-256 {attachment.sha256}</span>}
            </figcaption>
          </figure>
        ))}
      </div>
      {editable && (
        <form onSubmit={(event) => { void submit(event); }} className="mt-3 flex flex-wrap items-end gap-2 text-xs">
          <label>Image<input ref={fileInput} aria-label="Screenshot image" type="file" accept="image/png,image/jpeg,image/gif,image/webp" required onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
          <label>Caption<input aria-label="Screenshot caption" value={caption} required maxLength={500} onChange={(event) => setCaption(event.target.value)} className="ml-1 rounded bg-gray-800 p-1" /></label>
          <label>View ID<input aria-label="View ID" value={viewId} required maxLength={120} onChange={(event) => setViewId(event.target.value)} className="ml-1 rounded bg-gray-800 p-1" /></label>
          <label>Candidate ID<input aria-label="Candidate ID" value={candidateId} required maxLength={120} onChange={(event) => setCandidateId(event.target.value)} className="ml-1 rounded bg-gray-800 p-1" /></label>
          <button type="submit" disabled={attach.isPending} className="rounded bg-indigo-700 px-2 py-1 text-white disabled:opacity-50">Attach screenshot</button>
        </form>
      )}
      {error && <p role="alert" className="mt-2 text-xs text-red-300">{error}</p>}
    </section>
  );
}
