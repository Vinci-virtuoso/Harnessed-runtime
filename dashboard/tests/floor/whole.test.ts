/** Every branch of both files: a run over the floor in either scope. */
import { expect, it } from "vitest";
import { shout } from "./unvisited";
import { verdict } from "./subject";

it("lights every branch of both files", () => {
  expect(verdict("a")).toBe("alpha");
  expect(verdict("b")).toBe("beta");
  expect(verdict("c")).toBe("gamma");
  expect(verdict("d")).toBe("delta");
  expect(verdict("e")).toBe("other");
  expect(shout("a")).toBe("ALPHA");
  expect(shout("b")).toBe("BETA");
  expect(shout("c")).toBe("GAMMA");
  expect(shout("d")).toBe("DELTA");
  expect(shout("e")).toBe("OTHER");
});
