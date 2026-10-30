// Types for `eslint.config.mjs`, so `tests/unit/no-derivation.test.ts` can read the rule it tests.
import type { Linter } from "eslint";

export const NO_DERIVATION_ALLOWLIST: string[];
export const NO_DERIVATION: { selector: string; message: string }[];
declare const config: Linter.Config[];
export default config;
