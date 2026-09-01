import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import TaskAttachments from "../TaskAttachments";

const legacyFetch = vi.hoisted(() => vi.fn());
vi.mock("../../api/legacy-fetch", () => ({ legacyFetch }));

const META = {
  path: "/data/attachments/task-t1/abc123-shot.png",
  filename: "abc123-shot.png",
  content_type: "image/png",
  exists: true,
  size: 1234,
};

const json = (body: unknown, ok = true, status = 200) => ({
  ok,
  status,
  json: async () => body,
});

let client: QueryClient;
let listed: (typeof META)[];

beforeEach(() => {
  client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  listed = [];
  legacyFetch.mockReset().mockImplementation(async (_path: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      listed = [...listed, META];
      return json({ success: true, attachment: META, attachments: [META.path] });
    }
    if (init?.method === "DELETE") {
      listed = [];
      return json({ success: true, attachments: [], removed_from_disk: true });
    }
    return json({ success: true, attachments: listed });
  });
});
afterEach(() => {
  cleanup();
  client.clear();
});

function mount(props: { capturePaste?: boolean } = {}) {
  return render(
    <QueryClientProvider client={client}>
      <TaskAttachments taskId="t1" {...props} />
    </QueryClientProvider>,
  );
}

const pngFile = () => new File([new Uint8Array([137, 80, 78, 71])], "shot.png", { type: "image/png" });

const uploadCalls = () =>
  legacyFetch.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "POST");

describe("TaskAttachments", () => {
  it("uploads a pasted screenshot when capturePaste is on", async () => {
    mount({ capturePaste: true });
    const event = new Event("paste", { bubbles: true }) as ClipboardEvent;
    Object.defineProperty(event, "clipboardData", { value: { files: [pngFile()] } });
    await act(async () => {
      document.dispatchEvent(event);
    });

    await waitFor(() => expect(uploadCalls()).toHaveLength(1));
    const [url, init] = uploadCalls()[0] as [string, RequestInit];
    expect(url).toBe("/api/tasks/t1/attachments");
    const form = init.body as FormData;
    expect((form.get("file") as File).type).toBe("image/png");
    // Successful upload refreshes the list — the thumbnail appears.
    expect(await screen.findByAltText("abc123-shot.png")).toBeInTheDocument();
  });

  it("does not listen for paste by default", async () => {
    mount();
    const event = new Event("paste", { bubbles: true }) as ClipboardEvent;
    Object.defineProperty(event, "clipboardData", { value: { files: [pngFile()] } });
    await act(async () => {
      document.dispatchEvent(event);
    });
    expect(uploadCalls()).toHaveLength(0);
  });

  it("uploads files dropped on the dropzone", async () => {
    mount();
    fireEvent.drop(screen.getByTestId("attachment-dropzone"), {
      dataTransfer: { files: [pngFile()], types: ["Files"] },
    });
    await waitFor(() => expect(uploadCalls()).toHaveLength(1));
  });

  it("ignores dropped files with disallowed types", async () => {
    mount();
    const evil = new File(["x"], "evil.sh", { type: "text/x-shellscript" });
    fireEvent.drop(screen.getByTestId("attachment-dropzone"), {
      dataTransfer: { files: [evil], types: ["Files"] },
    });
    await new Promise((r) => setTimeout(r, 10));
    expect(uploadCalls()).toHaveLength(0);
  });

  it("renders existing attachments and deletes on remove", async () => {
    listed = [META];
    mount();
    expect(await screen.findByAltText("abc123-shot.png")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove abc123-shot.png" }));
    await waitFor(() => {
      const del = legacyFetch.mock.calls.find(
        ([, init]) => (init as RequestInit | undefined)?.method === "DELETE",
      );
      expect(del).toBeTruthy();
      expect(del![0]).toContain("/api/tasks/t1/attachment?path=");
      expect(del![0]).toContain(encodeURIComponent(META.path));
    });
    await waitFor(() =>
      expect(screen.queryByAltText("abc123-shot.png")).not.toBeInTheDocument(),
    );
  });

  it("surfaces upload errors", async () => {
    legacyFetch.mockImplementation(async (_path: string, init?: RequestInit) => {
      if (init?.method === "POST") return json({ detail: "file exceeds cap" }, false, 413);
      return json({ success: true, attachments: [] });
    });
    mount();
    fireEvent.drop(screen.getByTestId("attachment-dropzone"), {
      dataTransfer: { files: [pngFile()], types: ["Files"] },
    });
    expect(await screen.findByText("file exceeds cap")).toBeInTheDocument();
  });
});
