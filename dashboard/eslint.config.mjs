// Flat config. The decision 32 no-derivation rule keeps gateway values verbatim: the dashboard
// never computes, reorders, reformats or remembers what the gateway said (FR-49, D-1, D-6).
import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

/** The only files allowed arithmetic, by name: each works on the dashboard's clock, never a gateway
 * value untranslated - `lib/humanize.ts` takes one served `Leaf` at a time and renders it, never
 * combining two (decision 32 as amended). */
export const NO_DERIVATION_ALLOWLIST = ["lib/clock.ts", "lib/stream.ts", "lib/humanize.ts"];

const banned = (selector, message) => ({ selector, message: `${message} (decision 32)` });

const ARITHMETIC = ["-", "*", "/", "%", "**", "+"].map((op) =>
  banned(
    `BinaryExpression[operator="${op}"]`,
    `No \`${op}\`: render gateway values verbatim; strings use template literals`,
  ),
);
const COMPOUND = ["-=", "*=", "/=", "%=", "**=", "+="].map((op) =>
  banned(`AssignmentExpression[operator="${op}"]`, `No \`${op}\`: render gateway values verbatim`),
);
const REFORMAT = ["sort", "toSorted", "reverse", "toReversed", "reduce", "toFixed", "toPrecision"].map(
  (name) => banned(`CallExpression[callee.property.name="${name}"]`, `No \`.${name}()\`: keep the gateway order and text`),
);

/**
 * Decision 42 (FR-86): a `Leaf` may travel between components as a prop, because it carries its
 * own `route` and `path`; its text may not. `x.text` handed to a component's prop (an element
 * named in capitals), bare or inside a template, is a figure lifted out of its `Leaf`. A DOM
 * attribute is not a component's prop and `key` is React's.
 */
const COMPONENT_PROP =
  'JSXOpeningElement[name.name=/^[A-Z]/] > JSXAttribute[name.name!="key"] > JSXExpressionContainer';
const LEAF_TEXT = 'MemberExpression[property.name="text"]';
const LIFTED_TEXT = [`${COMPONENT_PROP} > ${LEAF_TEXT}`, `${COMPONENT_PROP} > TemplateLiteral > ${LEAF_TEXT}`].map(
  (selector) => ({
    selector,
    message: "No `leaf.text` in a component prop: a `Leaf` travels whole, never its text (decision 42)",
  }),
);

/** The one module allowed browser storage: the theme choice, a per-browser convenience (decision 85). */
export const STORAGE_ALLOWLIST = ["lib/theme.ts"];

const STORAGE = banned(
  "Identifier[name=/^(localStorage|sessionStorage|indexedDB)$/]",
  "No browser storage: the dashboard remembers nothing (D-6)",
);

export const NO_DERIVATION = [
  ...ARITHMETIC,
  ...COMPOUND,
  banned("UpdateExpression", "No `++` or `--`: iterate with `entries()` instead"),
  // A signed number literal (`tabIndex={-1}`) is a constant, not a derived value.
  banned('UnaryExpression[operator="-"]:not([argument.type="Literal"])', "No unary `-`"),
  banned('UnaryExpression[operator="+"]:not([argument.type="Literal"])', "No unary `+`"),
  banned("CallExpression[callee.name=/^(Number|parseFloat|parseInt)$/]", "No number parsing of gateway values"),
  banned(
    'MemberExpression[object.name="Number"][property.name=/^(parseFloat|parseInt)$/]',
    "No number parsing of gateway values",
  ),
  banned('MemberExpression[object.name="Math"]', "No `Math.*`"),
  ...REFORMAT,
  banned(
    'MemberExpression[object.name="Intl"][property.name="NumberFormat"]',
    "No `Intl.NumberFormat`: the gateway text is the display",
  ),
  STORAGE,
  banned(
    'MemberExpression[object.name="navigator"][property.name="serviceWorker"]',
    "No service worker: the dashboard remembers nothing (D-6)",
  ),
  ...LIFTED_TEXT,
  banned('Identifier[name="unstable_cache"]', "No `unstable_cache`: every read is live (decision 34)"),
  banned("ExpressionStatement[directive=/^use cache/]", 'No `"use cache"`: every read is live (decision 34)'),
];

export default defineConfig([
  ...nextVitals,
  ...nextTypescript,
  globalIgnores([".next/**", "node_modules/**", "test-results/**", "next-env.d.ts"]),
  {
    name: "harnessed/no-derivation",
    files: ["app/**", "components/**", "lib/**"],
    ignores: NO_DERIVATION_ALLOWLIST,
    rules: { "no-restricted-syntax": ["error", ...NO_DERIVATION] },
  },
  {
    name: "harnessed/storage-allowlist",
    files: STORAGE_ALLOWLIST,
    rules: { "no-restricted-syntax": ["error", ...NO_DERIVATION.filter((rule) => rule !== STORAGE)] },
  },
]);
