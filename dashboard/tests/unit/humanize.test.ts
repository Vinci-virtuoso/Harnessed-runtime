/**
 * `lib/humanize.ts` (decision 32 as amended, `D-1`): every human rendering pinned on a fake clock,
 * and the one hard rule - no export takes two `Leaf`s - checked against the module's own source
 * rather than against a sample call, so a signature that grew a second `Leaf` fails here even if
 * every existing call site still happens to compile.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { humanSeconds, humanSecondsWords, humanTimestamp } from "../../lib/humanize";
import type { Leaf } from "../../lib/wire";

const SOURCE = readFileSync(resolve(import.meta.dirname, "../../lib/humanize.ts"), "utf8");

/** A leaf of the one wire kind this file's exports read: a served string. */
function leaf(text: string): Leaf {
  return { kind: "string", text, path: ".at", route: "/v1/probe" };
}

describe("humanTimestamp (D-1 as amended)", () => {
  const NOW = new Date("2026-09-25T17:01:00.000Z");

  it('pins "2 min ago" for a timestamp two minutes before the dashboard clock, today', () => {
    const served = "2026-09-25T16:59:00.000Z";
    const human = humanTimestamp(leaf(served), NOW);
    expect(human.text).toBe("2 min ago");
    expect(human.exact).toBe(served);
  });

  it('pins "16:59" for a timestamp earlier today, beyond an hour ago', () => {
    const served = "2026-09-25T16:59:00.000Z";
    const human = humanTimestamp(leaf(served), new Date("2026-09-25T18:05:00.000Z"));
    expect(human.text).toBe("16:59");
    expect(human.exact).toBe(served);
  });

  it('pins "24 Sep, 16:59" for a timestamp on an earlier day', () => {
    const served = "2026-09-24T16:59:00.000Z";
    const human = humanTimestamp(leaf(served), NOW);
    expect(human.text).toBe("24 Sep, 16:59");
    expect(human.exact).toBe(served);
  });
});

describe("humanSeconds (D-1 as amended)", () => {
  it('pins "0.6 s" verbatim, under a minute', () => {
    const human = humanSeconds(leaf("0.6"));
    expect(human.text).toBe("0.6 s");
    expect(human.exact).toBe("0.6");
  });

  it('pins "2 min 3 s" at and past a minute', () => {
    const human = humanSeconds(leaf("123"));
    expect(human.text).toBe("2 min 3 s");
    expect(human.exact).toBe("123");
  });
});

describe("humanSecondsWords (FR-174)", () => {
  it('reads the served string in words: "0.4 seconds", "1 second"', () => {
    expect(humanSecondsWords(leaf("0.4")).text).toBe("0.4 seconds");
    expect(humanSecondsWords(leaf("0.4")).exact).toBe("0.4");
    expect(humanSecondsWords(leaf("1")).text).toBe("1 second");
  });
});

describe("no export takes two Leafs (D-1: never combine two served values)", () => {
  it("finds no exported function signature naming Leaf twice", () => {
    const signatures = [...SOURCE.matchAll(/export function \w+\(([^)]*)\)/g)].map(
      (match) => match[1],
    );
    expect(signatures.length).toBeGreaterThan(0);
    for (const signature of signatures) {
      const leafParams = signature.match(/:\s*Leaf\b/g) ?? [];
      expect(leafParams.length, signature).toBeLessThanOrEqual(1);
    }
  });
});
