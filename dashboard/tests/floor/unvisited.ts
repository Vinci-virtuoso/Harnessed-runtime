/**
 * The floor fixture's file that no test imports (ticket 118). Invisible in the narrow scope;
 * reported at 0% in the whole-surface scope, which is the only difference between the two.
 */
export function shout(word: string): string {
  if (word === "a") return "ALPHA";
  if (word === "b") return "BETA";
  if (word === "c") return "GAMMA";
  if (word === "d") return "DELTA";
  return "OTHER";
}
