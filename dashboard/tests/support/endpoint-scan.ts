/**
 * The two scans behind FR-126 (`D-9`, decision 68, MUST rule 31): no endpoint string is drawn.
 *
 * **The DOM scan** reads what a person or a screen reader gets - `textContent`, every `title`,
 * every accessible name and every copyable line - and reports any of the shapes below.
 * `collectStrings` runs in the page under Playwright, so it holds no closure and imports nothing.
 * The `data-route` and `data-path` pair on a `Value` button is the one place route text may sit in
 * the DOM, and it is never read here or anywhere else into text.
 *
 * **The static scan** reads the rendered string literals in `app/` and `components/` (JSX text, JSX
 * attribute values, and a literal inside a JSX expression). A request-building constant such as
 * `SESSIONS_ROUTE` is not rendered, so it is not read; the four request modules in `lib/` are the
 * named exemption and are never walked at all.
 *
 * Both call `endpointFindings`, so a seeded "GET /v1/runs" in a `title` fails both (MUST rule 12).
 */
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

import ts from "typescript";

/** One shape an operator must never see, named so a failure says which one was drawn. */
export const ENDPOINT_SHAPES: readonly { name: string; pattern: RegExp }[] = [
  { name: "gateway route", pattern: /\/v1\b/ },
  { name: "control route", pattern: /\/control\/v1\b/ },
  { name: "method prefix", pattern: /\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+\/?[\w{]/ },
  { name: "host and port", pattern: /(?:\b(?:localhost|127\.0\.0\.1)|\[::1\]|\b[a-z0-9-]+(?:\.[a-z0-9-]+)+):\d{2,5}\b/i },
  { name: "byte count", pattern: /\b\d[\d,.]*\s?(?:bytes?|KiB|KB|MiB|MB)\b/ },
  { name: "curl", pattern: /\bcurl\b/i },
  { name: "route to path", pattern: /\S\s*(?:->|→)\s*(?:\/|\$)/ },
  { name: "json path caption", pattern: /\$\.[A-Za-z_]|\b[A-Za-z_]\w*\[\d+\]\.[A-Za-z_]/ },
];

/** Every shape found in `text`, by name; empty means clean. */
export function endpointFindings(text: string): string[] {
  return ENDPOINT_SHAPES.filter(({ pattern }) => pattern.test(text)).map(({ name }) => name);
}

/**
 * Every string a viewer can receive from `root`: its text, and the attributes that become a name
 * or a tooltip. Self-contained so `page.evaluate(collectStrings)` can run it in the browser.
 */
export function collectStrings(root: ParentNode = document): string[] {
  const found: string[] = [];
  const text = (root as Element).textContent;
  if (text) found.push(text);
  for (const node of Array.from(root.querySelectorAll("*"))) {
    for (const name of ["title", "aria-label", "aria-description", "alt", "placeholder", "value"]) {
      const value = node.getAttribute(name);
      if (value) found.push(value);
    }
    const labelled = node.getAttribute("aria-labelledby");
    if (labelled) {
      for (const id of labelled.split(/\s+/)) {
        const label = node.ownerDocument.getElementById(id)?.textContent;
        if (label) found.push(label);
      }
    }
    // A copyable line is a `<code>` or an `<input>`/`<textarea>` value; both are text already.
    const typed = (node as HTMLInputElement).value;
    if (typeof typed === "string" && typed) found.push(typed);
  }
  return found;
}

/** Every finding in a set of strings, as `shape: excerpt`. */
export function scanStrings(strings: readonly string[]): string[] {
  return strings.flatMap((text) =>
    endpointFindings(text).map((shape) => `${shape}: ${text.slice(0, 80).replace(/\s+/g, " ")}`),
  );
}

/** Attributes whose value is request plumbing, never drawn. */
const PLUMBING = new Set(["route", "href", "data-route", "data-path", "className", "key", "id", "data-testid"]);

/** The rendered string literals of one source file. */
export function renderedLiterals(source: string, fileName = "file.tsx"): string[] {
  const tree = ts.createSourceFile(fileName, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const found: string[] = [];
  /**
   * Is this literal drawn? It is when it reaches a JSX expression container or a rendered attribute
   * through nothing but parentheses, a ternary, `&&`/`||` or a template. A literal handed to a call
   * (`wire.at(".events[0].name")`) is an argument, not a drawn string.
   */
  const inExpression = (node: ts.Node): boolean => {
    for (let at: ts.Node | undefined = node.parent; at !== undefined; at = at.parent) {
      if (ts.isJsxExpression(at)) return !ts.isJsxAttribute(at.parent) || !PLUMBING.has(at.parent.name.getText());
      const passes =
        ts.isParenthesizedExpression(at) ||
        ts.isConditionalExpression(at) ||
        ts.isBinaryExpression(at) ||
        ts.isTemplateExpression(at) ||
        ts.isTemplateSpan(at) ||
        ts.isAsExpression(at);
      if (!passes) return false;
    }
    return false;
  };
  const visit = (node: ts.Node): void => {
    if (ts.isJsxText(node)) found.push(node.text);
    else if (ts.isJsxAttribute(node) && !PLUMBING.has(node.name.getText())) {
      const value = node.initializer;
      if (value !== undefined && ts.isStringLiteral(value)) found.push(value.text);
    } else if (
      (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) &&
      !ts.isJsxAttribute(node.parent) &&
      inExpression(node)
    ) {
      found.push(node.text);
    } else if (ts.isTemplateExpression(node) && inExpression(node)) {
      // Whole, so `${leaf.route} -> ${leaf.path}` reads as the route-to-path caption it draws.
      found.push(node.getText());
    }
    ts.forEachChild(node, visit);
  };
  visit(tree);
  return found;
}

/** Source files under `directories` (relative to `root`), never the four exempt request modules. */
export function sourceFiles(root: string, directories: readonly string[]): string[] {
  return directories.flatMap((directory) =>
    readdirSync(resolve(root, directory), { recursive: true, encoding: "utf8" })
      .filter((entry) => /\.tsx?$/.test(entry))
      .map((entry) => resolve(root, directory, entry)),
  );
}

/** The static scan: every finding across the rendered literals of the given files. */
export function staticFindings(files: readonly string[]): string[] {
  return files.flatMap((file) =>
    scanStrings(renderedLiterals(readFileSync(file, "utf8"), file)).map((finding) => `${file}: ${finding}`),
  );
}
