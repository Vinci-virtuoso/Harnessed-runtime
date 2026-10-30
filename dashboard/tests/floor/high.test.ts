/** Every branch of `subject.ts`: a run over the floor in the narrow scope, under it in the whole. */
import { expect, it } from "vitest";
import { verdict } from "./subject";

it("lights every branch of the visited file", () => {
  expect(verdict("a")).toBe("alpha");
  expect(verdict("b")).toBe("beta");
  expect(verdict("c")).toBe("gamma");
  expect(verdict("d")).toBe("delta");
  expect(verdict("e")).toBe("other");
});
