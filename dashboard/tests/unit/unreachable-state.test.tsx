// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { UnreachableState } from "../../components/UnreachableState";

afterEach(cleanup);

describe("UnreachableState", () => {
  it("names harnessed serve as the fix, draws no URL, and takes focus", () => {
    render(<UnreachableState />);
    const heading = screen.getByRole("heading", { name: "Gateway unreachable" });
    expect(document.activeElement).toBe(heading);
    expect(document.body.textContent).not.toContain("/v1/");
    expect(screen.getByText("harnessed serve")).toBeTruthy();
    expect(screen.queryByTestId("unreachable-cause")).toBeNull();
  });

  it("quotes the fetch error when there is one", () => {
    render(<UnreachableState cause="ECONNREFUSED" />);
    expect(screen.getByTestId("unreachable-cause").textContent).toBe("ECONNREFUSED");
  });
});
