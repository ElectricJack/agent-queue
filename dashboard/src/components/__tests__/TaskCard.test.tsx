import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import TaskCard from "../TaskCard";
import type { Task } from "../../api/hooks";

const task = { id: "t1", project_id: "p1", title: "ｗ".repeat(200), status: "READY", priority: 3, assigned_agent: "worker-a" } as Task;

describe("TaskCard", () => {
  it("as a link: carries the row marker, wraps long titles and names the project", () => {
    render(<MemoryRouter><TaskCard task={task} projectName="First project" to="/focus/tasks/t1" /></MemoryRouter>);
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("href", "/focus/tasks/t1");
    expect(link).toHaveAttribute("data-task-row", "t1");
    expect(link).toHaveAttribute("data-primary-control");
    expect(screen.getByText("ｗ".repeat(200)).className).toContain("[overflow-wrap:anywhere]");
    expect(screen.getByText("First project")).toBeInTheDocument();
  });

  it("as a button: selects and reports selection", () => {
    const onSelect = vi.fn();
    render(<TaskCard task={task} selected onSelect={onSelect} />);
    const button = screen.getByRole("button");
    expect(button).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(button);
    expect(onSelect).toHaveBeenCalledOnce();
  });
});
