import { beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AskDecision } from "../AskDecision";
import { askData, preview } from "../../../testUtils/asks";
const wire = vi.hoisted(() => ({ prepare: vi.fn(), decide: vi.fn(), listener: undefined as undefined | ((e: { event_type: string }) => void) }));
vi.mock("../../../api/asks", () => ({ askKeys: { all: ["asks"], detail: (id: string) => ["asks", "detail", id] },
  usePrepareAsk: () => ({ mutateAsync: wire.prepare, isPending: false }), useDecideAsk: () => ({ mutateAsync: wire.decide, isPending: false }) }));
vi.mock("../../../ws/useEventStream", () => ({ useRawEventSubscription: (fn: typeof wire.listener) => { wire.listener = fn; } }));
beforeEach(() => { wire.prepare.mockReset().mockResolvedValue({ preview: preview() }); wire.decide.mockReset().mockResolvedValue({ success: true }); });
function mount(props: Partial<Parameters<typeof AskDecision>[0]> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryDefaults(["asks", "detail", "ask-one"], { queryFn: async () => askData() });
  return render(<QueryClientProvider client={client}><AskDecision data={askData()} {...props} /></QueryClientProvider>);
}
it("prepares without deciding and confirms the exact packet and version", async () => {
  mount(); fireEvent.click(screen.getByRole("button", { name: "Prepare Approve" }));
  fireEvent.click(await screen.findByRole("button", { name: "Confirm prepared decision" }));
  await waitFor(() => expect(wire.decide).toHaveBeenCalledWith(expect.objectContaining({ ask_id: "ask-one", expected: askData().expected, preview_id: "preview-one", option_key: "approve" })));
});
it("invalidates an open preview after remote feedback", async () => {
  mount(); fireEvent.click(screen.getByRole("button", { name: "Prepare Approve" }));
  await screen.findByRole("button", { name: "Confirm prepared decision" });
  act(() => wire.listener?.({ event_type: "review.commented" }));
  expect(screen.getByRole("button", { name: "Confirm prepared decision" })).toBeDisabled();
  expect(screen.getByRole("alert")).toHaveTextContent("stale"); expect(wire.decide).not.toHaveBeenCalled();
});
it("awaits every save before preparing and blocks on save failure", async () => {
  let finish!: () => void;
  const save = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
  mount({ draftBlocked: true, saveDraft: save });
  expect(screen.getByRole("button", { name: "Prepare Approve" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Save and prepare approval" }));
  expect(wire.prepare).not.toHaveBeenCalled(); expect(screen.getByText(/until every save/)).toBeInTheDocument();
  await act(async () => finish());
  await waitFor(() => expect(wire.prepare).toHaveBeenCalledTimes(1));
});
it("does not prepare after a rejected feedback save", async () => {
  mount({ draftBlocked: true, saveDraft: async () => { throw new Error("Save failed, draft retained"); } });
  fireEvent.click(screen.getByRole("button", { name: "Save and prepare approval" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Save failed");
  expect(wire.prepare).not.toHaveBeenCalled(); expect(wire.decide).not.toHaveBeenCalled();
});
it("uses server authority and explains a disabled choice", () => {
  const data = askData(); data.actions = [{ action: "decide", option_key: "approve", label: "Approve", enabled: false, disabled_reason: "Only the assigned decider" }];
  mount({ data }); expect(screen.getByRole("button", { name: "Prepare Approve" })).toBeDisabled();
  expect(screen.getByText("Only the assigned decider")).toBeInTheDocument();
});

it("keeps a decision note draft until acknowledgement and clears it after acceptance", async () => {
  mount(); fireEvent.change(screen.getByLabelText("Overall decision note"), { target: { value: "Preserve receipts" } });
  expect(screen.getByText(/Decision note draft/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Prepare Approve" }));
  expect(await screen.findByRole("button", { name: "Confirm prepared decision" })).toBeEnabled();
  expect(screen.getByLabelText("Overall decision note")).toHaveValue("Preserve receipts");
  fireEvent.click(screen.getByRole("button", { name: "Confirm prepared decision" }));
  await waitFor(() => expect(screen.getByLabelText("Overall decision note")).toHaveValue(""));
  expect(wire.decide).toHaveBeenCalledWith(expect.objectContaining({ overall_note: "Preserve receipts" }));
});
