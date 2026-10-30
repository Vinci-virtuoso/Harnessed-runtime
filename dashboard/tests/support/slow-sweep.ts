/**
 * The ceiling for a whole-corpus sweep (ticket 118).
 *
 * A sweep renders every recorded response in one test - about 50 renders, most with a provenance
 * drawer opened and closed - and finishes in a second or two when it runs alone. Under the full
 * unit suite every worker is busy and the same sweep passes the 5 s default, which is a ceiling
 * problem and not an assertion problem: thinning what is swept would weaken the claim, so the
 * ceiling moves instead.
 */
export const SLOW_SWEEP_MS = 60_000;
