"use client";

/**
 * A chat reply, an agent's text result, or a tool's text output (FR-127, `D-10`). A hand-written
 * reader - no markdown library, and no raw-HTML injection either - for paragraphs, lists,
 * emphasis and inline code; a fenced block renders through `CodeView`; nothing else is markdown
 * (design.md's `text` presenter row). Over 24 lines, only the first 24 show until "Show all".
 *
 * A `text` output whose characters happen to look like HTML render as this reader's plain
 * paragraph text - the characters are never parsed as markup, so no tag ever becomes an element.
 */
import { type ReactNode, useState } from "react";

import type { Leaf } from "../lib/wire";

import { CodeView } from "./CodeView";
import styles from "./TextView.module.css";

type Props = {
  output: Leaf;
};

/** Over this many lines, the block starts collapsed (design.md). */
const MAX_LINES = 24;

type Block =
  | { type: "code"; lang: string; text: string }
  | { type: "list"; ordered: boolean; items: string[] }
  | { type: "para"; text: string };

const FENCE = /```([^\n]*)\n([\s\S]*?)```/g;
const BULLET = /^[-*]\s+/;
const NUMBERED = /^\d+[.)]\s+/;
const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)/g;

function paragraphOrList(chunk: string): Block {
  const lines = chunk
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  if (lines.length > 0 && lines.every((line) => BULLET.test(line))) {
    return { type: "list", ordered: false, items: lines.map((line) => line.replace(BULLET, "")) };
  }
  if (lines.length > 0 && lines.every((line) => NUMBERED.test(line))) {
    return { type: "list", ordered: true, items: lines.map((line) => line.replace(NUMBERED, "")) };
  }
  // A paragraph keeps its own internal line breaks verbatim (`white-space: pre-wrap` in the
  // module CSS renders them), rather than joining soft-wrapped lines into one: a tool's own text
  // output is not prose written to a column width, and collapsing its newlines would be lossy.
  return { type: "para", text: chunk.trim() };
}

function paragraphBlocks(text: string): Block[] {
  return text
    .split(/\n\s*\n/)
    .map((chunk) => chunk.trim())
    .filter((chunk) => chunk.length > 0)
    .map((chunk) => paragraphOrList(chunk));
}

/**
 * Fenced code blocks pulled out first, so a blank line inside one never splits a paragraph.
 * `exec`'s own `lastIndex` tracks where the next search resumes - no arithmetic on a match's
 * index and length, which the no-derivation lint would ban in this tree (decision 32).
 */
function blocksOf(text: string): Block[] {
  const blocks: Block[] = [];
  const fence = new RegExp(FENCE.source, "g");
  let cursor = 0;
  let match = fence.exec(text);
  while (match !== null) {
    blocks.push(...paragraphBlocks(text.slice(cursor, match.index)));
    blocks.push({ type: "code", lang: match[1] ?? "", text: match[2] ?? "" });
    cursor = fence.lastIndex;
    match = fence.exec(text);
  }
  blocks.push(...paragraphBlocks(text.slice(cursor)));
  return blocks;
}

/** `**bold**`, `` `code` `` and `*italic*`, the only emphasis design.md keeps. */
function inline(text: string): ReactNode[] {
  return text
    .split(INLINE)
    .filter((part) => part.length > 0)
    .map((part, index) => {
      if (part.startsWith("**") && part.endsWith("**")) {
        return <strong key={String(index)}>{part.slice(2, -2)}</strong>;
      }
      if (part.startsWith("`") && part.endsWith("`")) {
        return <code key={String(index)}>{part.slice(1, -1)}</code>;
      }
      if (part.startsWith("*") && part.endsWith("*")) {
        return <em key={String(index)}>{part.slice(1, -1)}</em>;
      }
      return <span key={String(index)}>{part}</span>;
    });
}

export function TextView({ output }: Props) {
  const lines = output.text.split("\n");
  const collapsible = lines.length > MAX_LINES;
  const [expanded, setExpanded] = useState(false);
  const whole = expanded || !collapsible;
  const shown = whole ? output.text : lines.slice(0, MAX_LINES).join("\n");
  const blocks = blocksOf(shown);

  return (
    <div className={styles.text} data-testid="text-view">
      {blocks.map((block, index) => {
        const key = String(index);
        if (block.type === "code") {
          const codeLeaf: Leaf = {
            kind: "string",
            text: block.text,
            path: output.path,
            route: output.route,
          };
          return <CodeView key={key} output={codeLeaf} />;
        }
        if (block.type === "list") {
          return block.ordered ? (
            <ol key={key}>
              {block.items.map((item, itemIndex) => (
                <li key={String(itemIndex)}>{inline(item)}</li>
              ))}
            </ol>
          ) : (
            <ul key={key}>
              {block.items.map((item, itemIndex) => (
                <li key={String(itemIndex)}>{inline(item)}</li>
              ))}
            </ul>
          );
        }
        return <p key={key}>{inline(block.text)}</p>;
      })}
      {collapsible ? (
        <button
          type="button"
          className={styles.showAll}
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "Collapse" : "Show all"}
        </button>
      ) : null}
    </div>
  );
}
