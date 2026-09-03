import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import EventScopeSelector from "../EventScopeSelector";
import { graph } from "./fixtures";

afterEach(cleanup);

describe("EventScopeSelector", () => {
  it("lists every event group and an All events option, behind a real label", () => {
    render(<EventScopeSelector groups={graph.event_groups!} value="" onChange={vi.fn()} />);
    const select = screen.getByLabelText("Event scope");
    expect(select).toBe(screen.getByRole("combobox"));
    const options = screen.getAllByRole("option").map((o) => o.textContent);
    expect(options[0]).toBe("All events (13 steps)");
    expect(options).toHaveLength(graph.event_groups!.length + 1);
    expect(options[1]).toContain("task.completed");
    expect(options[1]).toContain("7 steps");
    expect(options[2]).toContain("spec.approved");
  });

  it("reports the selected event type", async () => {
    const onChange = vi.fn();
    render(<EventScopeSelector groups={graph.event_groups!} value="" onChange={onChange} />);
    await userEvent.selectOptions(screen.getByLabelText("Event scope"), "spec.approved");
    expect(onChange).toHaveBeenCalledWith("spec.approved");
  });

  it("shows the current scope as the selected option", () => {
    render(<EventScopeSelector groups={graph.event_groups!} value="spec.approved" onChange={vi.fn()} />);
    expect((screen.getByLabelText("Event scope") as HTMLSelectElement).value).toBe("spec.approved");
  });
});
