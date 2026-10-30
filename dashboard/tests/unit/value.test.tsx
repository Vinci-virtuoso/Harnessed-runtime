// @vitest-environment jsdom
/**
 * FR-129 (`D-3`), FR-134: a null reason explains an absence, so `Value` renders it beside a null
 * leaf only. A caller passes the reason unconditionally; beside a value it would be a false claim.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Value } from "../../components/Value";
import type { Leaf } from "../../lib/wire";

const REASON = "no OpenRouter agent reported a cost";

const leaf = (kind: Leaf["kind"], text: string): Leaf => ({
  kind,
  text,
  path: ".cost",
  route: "/v1/spend",
});

afterEach(cleanup);

describe("Value renders a null reason beside an unknown only", () => {
  it("renders 0 text of the reason beside a value, and keeps its name", () => {
    const { container } = render(<Value leaf={leaf("number", "0.42")} nullReason={REASON} />);
    expect(container.textContent).toBe("0.42");
    expect(screen.queryByText(REASON)).toBeNull();
    const figure = container.querySelector("[data-figure]");
    expect(figure?.getAttribute("aria-label")).toBeNull();
  });

  it("renders unknown and the reason beside a null", () => {
    const { container } = render(<Value leaf={leaf("null", "null")} nullReason={REASON} />);
    expect(container.textContent).toBe(`unknown${REASON}`);
    expect(screen.getByText(REASON)).toBeTruthy();
    expect(container.querySelector("[data-figure]")?.getAttribute("aria-label")).toBe(
      `unknown, ${REASON}`,
    );
  });
});

describe("ticket 284: a wrapped figure keeps its left edge", () => {
  it("sets text-align on .figure and button.figure:hover so a button does not centre its wrapped lines", () => {
    const css = readFileSync(join(__dirname, "../../components/Value.module.css"), "utf8");
    const rule = (selector: string) =>
      css.match(new RegExp(`(?:^|\\n)${selector.replace(/[.:]/g, "\\$&")}\\s*\\{([^}]*)\\}`))?.[1] ?? "";
    expect(rule(".figure")).toMatch(/text-align:\s*(inherit|start)/);
    expect(rule("button.figure:hover")).toMatch(/text-align:\s*(inherit|start)/);
  });
});
