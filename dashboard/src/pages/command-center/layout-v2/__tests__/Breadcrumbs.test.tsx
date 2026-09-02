import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import Breadcrumbs from "../Breadcrumbs";

describe("Breadcrumbs", () => {
  it("renders the path and navigates", async () => {
    const onSelect = vi.fn();
    render(<Breadcrumbs projectName="agent-queue" ancestors={[{ id: "e", title: "Epic" }]} current={{ id: "p", title: "Pkg" }} onSelect={onSelect} />);
    expect(screen.getByText("Pkg")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Epic" }));
    expect(onSelect).toHaveBeenCalledWith("e");
    await userEvent.click(screen.getByRole("button", { name: "agent-queue" }));
    expect(onSelect).toHaveBeenCalledWith(null);
  });
});
