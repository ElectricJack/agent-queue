import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import SettingsSidebar from "../../../components/nav/SettingsSidebar";

describe("settings navigation", () => {
  it("links operators to project root settings", () => {
    render(<MemoryRouter><SettingsSidebar /></MemoryRouter>);

    expect(screen.getByRole("link", { name: "Project roots" })).toHaveAttribute(
      "href",
      "/project-roots",
    );
  });
});
