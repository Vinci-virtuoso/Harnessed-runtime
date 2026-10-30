/** One branch of `subject.ts` and nothing else: a run under the floor. */
import { expect, it } from "vitest";
import { verdict } from "./subject";

it("lights one branch", () => {
  expect(verdict("a")).toBe("alpha");
});
