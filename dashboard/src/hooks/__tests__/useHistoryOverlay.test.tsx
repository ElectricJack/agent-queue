import { describe, expect, it } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { useHistoryOverlay } from "../useHistoryOverlay";

function Probe() {
  const menu = useHistoryOverlay("menu");
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <>
      <output aria-label="open">{String(menu.open)}</output>
      <output aria-label="path">{location.pathname}</output>
      <button onClick={menu.show}>show</button>
      <button onClick={menu.hide}>hide</button>
      <button onClick={() => navigate("/elsewhere")}>go</button>
      <button onClick={() => navigate(-1)}>back</button>
    </>
  );
}

const renderProbe = () => render(<MemoryRouter initialEntries={["/page"]}><Probe /></MemoryRouter>);
const click = (name: string) => act(() => screen.getByRole("button", { name }).click());

describe("useHistoryOverlay", () => {
  it("opens on a pushed entry, so Back closes it", () => {
    renderProbe();
    click("show");
    expect(screen.getByLabelText("open")).toHaveTextContent("true");
    click("back");
    expect(screen.getByLabelText("open")).toHaveTextContent("false");
    expect(screen.getByLabelText("path")).toHaveTextContent("/page");
  });

  it("hide goes back over its own entry", () => {
    renderProbe();
    click("show");
    click("hide");
    expect(screen.getByLabelText("open")).toHaveTextContent("false");
  });

  it("does not reopen on Back after navigating away from inside it", () => {
    renderProbe();
    click("show");
    click("go");
    expect(screen.getByLabelText("path")).toHaveTextContent("/elsewhere");
    click("back");
    expect(screen.getByLabelText("path")).toHaveTextContent("/page");
    expect(screen.getByLabelText("open")).toHaveTextContent("false");
  });
});
