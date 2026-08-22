import { render, screen } from "@testing-library/react";
import { PANE_REGISTRY } from "../registry";

test("stub-smoke view is registered and renders", () => {
  const entry = PANE_REGISTRY["__stub-smoke"];
  expect(entry).toBeDefined();
  const { Component } = entry!;
  render(
    <Component
      args={{ text: "hello pane" }}
      close={() => {}}
      setArgs={() => {}}
      setToolbar={() => {}}
      setShortcuts={() => {}}
    />,
  );
  expect(screen.getByTestId("stub-smoke")).toHaveTextContent("hello pane");
});
