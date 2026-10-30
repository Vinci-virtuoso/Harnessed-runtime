/**
 * The floor fixture's visited file (ticket 118). Five branches, so a test that lights one lands
 * far under the floor and a test that lights all five lands at 100%.
 */
export function verdict(word: string): string {
  if (word === "a") return "alpha";
  if (word === "b") return "beta";
  if (word === "c") return "gamma";
  if (word === "d") return "delta";
  return "other";
}
