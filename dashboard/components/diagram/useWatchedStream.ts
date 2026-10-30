import { useContext, useEffect, useMemo, useRef, useState } from "react";

import { RefreshContext } from "../PanelFrame";
import { fetchPanel } from "../../lib/fetch-panel";
import { leafIfSent, rowsOf } from "../../lib/rows";
import {
  type StreamFrame,
  type StreamStatus,
  nextBackoffMs,
  readStream,
  streamRoute,
} from "../../lib/stream";
import type { Leaf } from "../../lib/wire";

/** The frames that mean a count may have moved (FR-75). Any other name is not a signal. */
export const REFETCH_SIGNALS: ReadonlySet<string> = new Set([
  "run.finished",
  "eval.run.finished",
  "improve.run.finished",
  "memory.write.committed",
  "task.paused",
  "task.finished",
  "memory.consolidation.finished",
]);

/** The listing the watched session is chosen from: its first row is the newest session. */
export const SESSIONS_ROUTE = "/v1/sessions";

/** The registry, read once for the diagram's delegation boxes (FR-124): never a refetch signal,
 * since the registry is config, not a run's own state. */
export const AGENTS_ROUTE = "/v1/agents";

/** What the watch line says, including the two states before a stream exists. */
export type Watch =
  | { kind: "stream"; status: StreamStatus }
  | { kind: "none" }
  | { kind: "unanswered"; status: string };

/**
 * Resolve the newest session, hold its stream open, and hand back a fresh token per signal.
 *
 * The session id is the listing's own leaf, placed into the stream route verbatim (MUST rule 25).
 * The cursor a reconnect sends is the last frame's own `seq` text (MUST rule 21).
 */
export function useWatchedStream(origin: string): {
  refresh: object | undefined;
  session: Leaf | undefined;
  watch: Watch;
  frames: readonly StreamFrame[];
} {
  const { token } = useContext(RefreshContext);
  const [signal, setSignal] = useState<object | undefined>(undefined);
  // A stream signal or the shell's Refresh: either is one more read.
  const refresh = useMemo(() => ({ signal, token }), [signal, token]);
  const [frames, setFrames] = useState<readonly StreamFrame[]>([]);
  const [session, setSession] = useState<Leaf | undefined>(undefined);
  const [watch, setWatch] = useState<Watch>({ kind: "stream", status: { kind: "connecting" } });
  // A fresh token is what a reconnect is: a new subscription, not an increment of an old one.
  const [attempt, setAttempt] = useState<object>({});
  const cursor = useRef<string | undefined>(undefined);
  const waited = useRef<number | undefined>(undefined);
  const held = useRef<string | undefined>(undefined);

  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    void fetchPanel(SESSIONS_ROUTE, origin, controller.signal).then((result) => {
      if (!live) return;
      if (result.state !== "ok") {
        setWatch({
          kind: "unanswered",
          status: result.state === "unreachable" ? "unreachable" : result.response.status,
        });
        return;
      }
      const newest = leafIfSent(result.wire, ".sessions[0].session_id");
      if (newest === undefined) {
        setWatch({ kind: "none" });
        return;
      }
      // The same session again is not a new subscription: keep the held leaf.
      if (held.current === newest.text) return;
      // A different session starts its own stream: the old cursor and frames do not carry over.
      held.current = newest.text;
      cursor.current = undefined;
      waited.current = undefined;
      setFrames([]);
      setSession(newest);
    });
    return () => {
      live = false;
      controller.abort();
    };
  }, [origin, refresh]);

  useEffect(() => {
    if (session === undefined) return undefined;
    const controller = new AbortController();
    let live = true;
    let retry: ReturnType<typeof setTimeout> | undefined;

    void readStream(
      streamRoute(session.text),
      cursor.current,
      (signal) => {
        if (!live) return;
        if (signal.kind === "frames") {
          for (const frame of signal.frames) cursor.current = frame.seq.text;
          waited.current = undefined;
          setFrames((arrived) => [...arrived, ...signal.frames]);
          setWatch({ kind: "stream", status: { kind: "live" } });
          if (signal.frames.some((frame) => REFETCH_SIGNALS.has(frame.name.text))) {
            setSignal({});
          }
          return;
        }
        if (signal.kind === "refused") {
          setWatch({ kind: "stream", status: signal });
          return;
        }
        if (signal.kind === "closed") {
          setWatch({ kind: "stream", status: { kind: "closed", reason: "the stream ended" } });
          return;
        }
        const wait = nextBackoffMs(waited.current);
        waited.current = wait;
        setWatch({
          kind: "stream",
          status: { kind: "reconnecting", cursor: cursor.current ?? "none" },
        });
        retry = setTimeout(() => {
          if (live) setAttempt({});
        }, wait);
      },
      controller.signal,
    );

    return () => {
      live = false;
      controller.abort();
      if (retry !== undefined) clearTimeout(retry);
    };
  }, [session, attempt]);

  return { refresh, session, watch, frames };
}

/**
 * `/v1/agents`' own `.agent` `Leaf`s, in served order, for the diagram's delegation boxes
 * (FR-124). Read on load and on the page's refetch signal (ticket 205). With a `kind`, only the
 * entries the registry serves under that `kind` (the Memory diagram draws the `cli` ones).
 */
export function useAgentEntries(
  origin: string,
  refresh: object | undefined,
  kind?: string,
): readonly Leaf[] {
  const { token } = useContext(RefreshContext);
  const [entries, setEntries] = useState<readonly Leaf[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    void fetchPanel(AGENTS_ROUTE, origin, controller.signal).then((result) => {
      if (!live || result.state !== "ok") return;
      const wire = result.wire;
      const rows = rowsOf(wire, (index) => `.agents[${index}].agent`).filter(
        (row) => kind === undefined || wire.at(`.agents[${row}].kind`).text === kind,
      );
      setEntries(rows.map((row) => result.wire.at(`.agents[${row}].agent`)));
    });
    return () => {
      live = false;
      controller.abort();
    };
  }, [origin, refresh, token, kind]);
  return entries;
}

