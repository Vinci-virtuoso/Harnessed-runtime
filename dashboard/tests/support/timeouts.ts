/**
 * Per-case time budgets that are slow by nature (ticket 230, after ticket 228). The global
 * `testTimeout` in `vitest.config.ts` stays at vitest's default; a case that needs more declares it
 * beside the case, with the reason, and imports the number from here so no file invents its own.
 */

/**
 * A case that builds `ESLint` and lints the source tree. A cold type-aware start outruns the 5 s
 * default on a slow host, and it is the same wait on every machine that has not linted yet.
 */
export const LINT_CASE_TIMEOUT_MS = 60_000;
