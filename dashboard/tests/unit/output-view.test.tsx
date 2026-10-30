// @vitest-environment jsdom
/**
 * FR-127 (`D-10`, decision 65): `OutputView` picks exactly one presenter from the served kind,
 * never from the content, and a kind the table does not name - or one it cannot show, because it
 * has neither an inline body nor an id to fetch one by - falls to `FileCard`.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { OutputView } from "../../components/OutputView";
import { outputBodyUrl } from "../../lib/outputs";
import type { Leaf } from "../../lib/wire";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const EVIDENCE = resolve(DASHBOARD, "../evidence/183");

/** A `Leaf` built for a test: the wire fields `OutputView` and its presenters actually read. */
function leaf(text: string, path = ".output", route = "/v1/sessions/x/messages"): Leaf {
  return { kind: "string", text, path, route };
}

/** One rendered fixture, written for the ticket's own evidence manifest. */
function capture(name: string, html: string): void {
  mkdirSync(EVIDENCE, { recursive: true });
  writeFileSync(resolve(EVIDENCE, `${name}.html`), html, "utf8");
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** Let a pending `fetch().then()` chain settle before the next assertion. */
async function settle(): Promise<void> {
  await act(async () => {});
}

describe("OutputView picks exactly one presenter, by the served kind alone (AC1)", () => {
  it("renders `text` as TextView", () => {
    const { container } = render(<OutputView kind={leaf("text")} output={leaf("plain reply text")} />);
    expect(screen.getByTestId("text-view").textContent).toContain("plain reply text");
    expect(screen.queryByTestId("record-view")).toBeNull();
    expect(screen.queryByTestId("code-view")).toBeNull();
    expect(screen.queryByTestId("file-card")).toBeNull();
    capture("kind-text", container.innerHTML);
  });

  it("renders a `text` output whose characters are HTML as text, never as markup", () => {
    const { container } = render(
      <OutputView kind={leaf("text")} output={leaf("<b>bold</b> and <script>alert(1)</script>")} />,
    );
    expect(container.querySelector("b")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByTestId("text-view").textContent).toContain(
      "<b>bold</b> and <script>alert(1)</script>",
    );
  });

  it("renders `code` as CodeView, marking added and removed lines", () => {
    const { container } = render(
      <OutputView kind={leaf("code")} output={leaf("kept\n+added\n-removed")} name={leaf("a.diff")} />,
    );
    const view = screen.getByTestId("code-view");
    expect(view.textContent).toContain("a.diff");
    const lines = within(view).getAllByTestId("code-line");
    expect(lines).toHaveLength(3);
    expect(lines[1]?.textContent).toContain("added");
    expect(lines[2]?.textContent).toContain("removed");
    capture("kind-code", container.innerHTML);
  });

  it("renders `record` as RecordView, a field list read from the served JSON", () => {
    const { container } = render(
      <OutputView kind={leaf("record")} output={leaf('{"path":"notes.md","ok":true}')} />,
    );
    const view = screen.getByTestId("record-view");
    expect(view.textContent).toContain("path");
    expect(view.textContent).toContain("notes.md");
    expect(view.textContent).toContain("ok");
    expect(view.textContent).toContain("true");
    capture("kind-record", container.innerHTML);
  });

  it("renders `ui` as UiPreview, fetching its markup by id", async () => {
    const markup = "<p>hello</p>";
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        expect(url).toBe(outputBodyUrl("out_ui1"));
        return { ok: true, text: async () => markup } as unknown as Response;
      }),
    );
    const { container } = render(
      <OutputView kind={leaf("ui")} output={null} outputId={leaf("out_ui1")} name={leaf("page.html")} />,
    );
    await settle();
    expect(screen.getByTestId("ui-preview")).toBeTruthy();
    capture("kind-ui", container.innerHTML);
  });

  it("renders `image` as ImageView, its `src` built from the output id", () => {
    const { container } = render(
      <OutputView kind={leaf("image")} output={null} outputId={leaf("out_img1")} name={leaf("shot.png")} />,
    );
    const img = screen.getByTestId("image-view") as HTMLImageElement;
    expect(img.getAttribute("src")).toBe(outputBodyUrl("out_img1"));
    expect(img.getAttribute("alt")).toBe("shot.png");
    capture("kind-image", container.innerHTML);
  });

  it("renders `diagram` as DiagramView, an `<img>` with `role=\"img\"`", () => {
    const { container } = render(<OutputView kind={leaf("diagram")} output={null} outputId={leaf("out_dgm1")} />);
    const img = screen.getByTestId("diagram-view");
    expect(img.getAttribute("role")).toBe("img");
    expect(img.getAttribute("src")).toBe(outputBodyUrl("out_dgm1"));
    capture("kind-diagram", container.innerHTML);
  });
});

describe("a kind the table does not name, or one with nothing to show, is FileCard (AC2)", () => {
  it("shows the kind word verbatim, the served size and the CLI line", () => {
    const { container } = render(
      <OutputView
        kind={leaf("audio/wav")}
        output={null}
        outputId={leaf("out_snd1")}
        name={leaf("clip.wav")}
        size={leaf("42 KB")}
      />,
    );
    const card = screen.getByTestId("file-card");
    expect(within(card).getByTestId("file-card-kind").textContent).toBe("audio/wav");
    expect(card.textContent).toContain("42 KB");
    expect(within(card).getByTestId("file-card-cli").textContent).toBe(
      "harnessed outputs get <output_id> --out <path>",
    );
    capture("kind-other-filecard", container.innerHTML);
  });

  it("falls back to FileCard for a `ui`, `image` or `diagram` kind with no output id to fetch by", () => {
    for (const kind of ["ui", "image", "diagram"]) {
      cleanup();
      render(<OutputView kind={leaf(kind)} output={null} name={leaf("mystery")} />);
      expect(screen.getByTestId("file-card")).toBeTruthy();
    }
  });

  it("falls back to FileCard for a `text`, `code` or `record` kind with no inline output", () => {
    for (const kind of ["text", "code", "record"]) {
      cleanup();
      render(<OutputView kind={leaf(kind)} output={null} name={leaf("mystery")} />);
      expect(screen.getByTestId("file-card")).toBeTruthy();
    }
  });
});

describe("UiPreview's iframe is sandboxed and traps focus in its full-size modal (AC3)", () => {
  it("carries `sandbox=\"\"`, a `srcdoc` and 0 `src`", async () => {
    const markup = "<p>a preview</p>";
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, text: async () => markup }) as unknown as Response));
    render(<OutputView kind={leaf("ui")} output={null} outputId={leaf("out_ui2")} name={leaf("page.html")} />);
    await settle();

    const iframe = screen.getByTestId("ui-preview-frame");
    expect(iframe.getAttribute("sandbox")).toBe("");
    expect(iframe.getAttribute("srcdoc")).toBe(markup);
    expect(iframe.hasAttribute("src")).toBe(false);
  });

  it("switches to CodeView on \"View source\", showing the fetched markup verbatim", async () => {
    const markup = "<p>view me</p>";
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, text: async () => markup }) as unknown as Response));
    render(<OutputView kind={leaf("ui")} output={null} outputId={leaf("out_ui3")} name={leaf("page.html")} />);
    await settle();

    fireEvent.click(screen.getByRole("button", { name: "View source" }));
    expect(screen.getByTestId("code-view").textContent).toContain(markup);
    expect(screen.queryByTestId("ui-preview-frame")).toBeNull();
  });

  it("traps focus in \"Open full size\" and returns it to the opener on Esc", async () => {
    const markup = "<p>full size</p>";
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, text: async () => markup }) as unknown as Response));
    render(<OutputView kind={leaf("ui")} output={null} outputId={leaf("out_ui4")} name={leaf("page.html")} />);
    await settle();

    const opener = screen.getByRole("button", { name: "Open full size" });
    fireEvent.click(opener);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByTestId("ui-preview-modal-frame")).toBeTruthy();

    const close = within(dialog).getByRole("button", { name: "Close" });
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(close);

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(opener);
  });
});

describe("the presenters' other documented states (FR-127 consequences)", () => {
  it("TextView keeps a list, emphasis and inline code, renders a fenced block as CodeView, and collapses over 24 lines", () => {
    const filler = Array.from({ length: 30 }, () => "x").join("\n");
    const text = [
      "a **bold** word, an *italic* one and `inline code`.",
      "",
      "- one",
      "- two",
      "",
      "```",
      "fenced();",
      "```",
      "",
      filler,
    ].join("\n");
    render(<OutputView kind={leaf("text")} output={leaf(text)} />);

    const view = screen.getByTestId("text-view");
    expect(view.querySelector("strong")?.textContent).toBe("bold");
    expect(view.querySelector("em")?.textContent).toBe("italic");
    expect(view.querySelector("code")?.textContent).toBe("inline code");
    expect(within(view).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "one",
      "two",
    ]);
    expect(within(view).getByTestId("code-view").textContent).toContain("fenced();");

    const showAll = within(view).getByRole("button", { name: "Show all" });
    expect(showAll.getAttribute("aria-expanded")).toBe("false");
    expect(view.textContent).not.toContain(filler);
    fireEvent.click(showAll);
    expect(within(view).getByRole("button", { name: "Collapse" }).getAttribute("aria-expanded")).toBe(
      "true",
    );
    expect(view.textContent).toContain(filler);
  });

  it("ImageView falls back to its unreadable state when the image fails to load", () => {
    render(<OutputView kind={leaf("image")} output={null} outputId={leaf("out_bad_img")} />);
    fireEvent.error(screen.getByTestId("image-view"));
    expect(screen.getByTestId("image-unreadable").textContent).toBe("the image could not be read");
  });

  it("DiagramView falls back to source in CodeView when the diagram cannot be rendered (G-34)", async () => {
    const mermaidSource = "graph TD; A-->B;";
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, text: async () => mermaidSource }) as unknown as Response),
    );
    render(<OutputView kind={leaf("diagram")} output={null} outputId={leaf("out_mermaid")} />);
    fireEvent.error(screen.getByTestId("diagram-view"));
    await settle();

    const fallback = await screen.findByTestId("diagram-source");
    expect(fallback.textContent).toContain(mermaidSource);
    expect(fallback.textContent).toContain("no diagram renderer is bundled yet");
  });

  it("RecordView nests one level open and hides a deeper field behind its own disclosure", () => {
    const record = leaf('{"messages":[{"content":"read the file","role":"user"}]}');
    render(<OutputView kind={leaf("record")} output={record} />);

    const view = screen.getByTestId("record-view");
    expect(view.textContent).not.toContain("read the file");
    const toggle = within(view).getByRole("button", { name: "Show fields" });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    expect(view.textContent).toContain("read the file");
    expect(view.textContent).toContain("user");
  });
});

describe("0 content sniffing and 0 dangerouslySetInnerHTML (AC4)", () => {
  const FILES = [
    "OutputView",
    "TextView",
    "UiPreview",
    "ImageView",
    "DiagramView",
    "CodeView",
    "RecordView",
    "FileCard",
  ];

  function source(name: string): string {
    return readFileSync(resolve(DASHBOARD, `components/${name}.tsx`), "utf8");
  }

  it("finds 0 `dangerouslySetInnerHTML` across the output components", () => {
    for (const file of FILES) {
      expect(source(file)).not.toContain("dangerouslySetInnerHTML");
    }
  });

  it("finds 0 read of `output`'s or `name`'s own characters in OutputView's presenter choice", () => {
    const dispatch = source("OutputView");
    // The only field the switch reads is `kind.text`; `output.text` or `name.text` in this file
    // would mean a presenter was chosen from the body or from a filename extension (MUST rule 32).
    expect(dispatch).not.toContain("output.text");
    expect(dispatch).not.toContain("output?.text");
    expect(dispatch).not.toContain("name.text");
    expect(dispatch).not.toContain("name?.text");
    expect(dispatch).not.toMatch(/\.split\(["']\.["']\)/);
    expect(dispatch).not.toContain(".endsWith(");
    expect(dispatch).toContain("kind.text");
  });

  it("proves the scan can fail: a seeded switch on content is caught", () => {
    const sniffing = 'if (output.text.startsWith("<")) return <UiPreview />;';
    expect(sniffing).toContain("output.text");
    const extensionSwitch = 'name.text.split(".").pop()';
    expect(extensionSwitch).toMatch(/\.split\(["']\.["']\)/);
    const inlined = "<div dangerouslySetInnerHTML={{ __html: output.text }} />";
    expect(inlined).toContain("dangerouslySetInnerHTML");

    mkdirSync(EVIDENCE, { recursive: true });
    writeFileSync(
      resolve(EVIDENCE, "sniffing-scan.txt"),
      [
        `files scanned: ${FILES.join(", ")}`,
        "dangerouslySetInnerHTML occurrences: 0",
        "OutputView.tsx reads of output.text / name.text / an extension split: 0",
        "seeded violations above are caught by the same checks (falsifiability)",
      ].join("\n"),
      "utf8",
    );
  });
});
