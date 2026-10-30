"use client";

/**
 * The console's main column: one `Exchange` per served message, in served order (FR-122, FR-128,
 * UJ-16; design.md "The console in v5", decision 66). The browser builds this list from exactly
 * one read, `GET /v1/sessions/{id}/messages` - **0 message lists built from events** - joined with
 * a second read, `GET /v1/sessions/{id}/decisions`, sliced per message on the served `message_seq`
 * (MUST rule 26's "the runtime carries the session's messages" extended to the browser's own
 * render: nothing here replays the event log to find out what was said).
 *
 * The two reads nest so every figure keeps its own source: the decisions `PanelFrame` is
 * outermost, and its own `usePanel()` context is captured once and handed to every `Exchange` as
 * `decisionsPanel`, which each exchange uses to re-open that context (`ResponseSource`) around
 * exactly its decision-derived figures. The messages `PanelFrame` nests inside it, so the *ambient*
 * context every other figure in an `Exchange` inherits is the messages response - the correct
 * default, since most of an exchange's figures (the reply, the caption) come from it.
 */
import { leafIfSent, rowsOf } from "../lib/rows";
import { useContext, useEffect, useRef, useState } from "react";

import { type PanelResult, fetchPanel } from "../lib/fetch-panel";
import type { Leaf, Wire } from "../lib/wire";

import { Composer, type Opened, type TaskState } from "./Composer";
import { ErrorState } from "./ErrorState";
import { Exchange } from "./Exchange";
import { announcement } from "./PermissionCard";
import { GatewayOriginContext, PanelFrame, type PanelContextValue, usePanel } from "./PanelFrame";
import { UnreachableState } from "./UnreachableState";
import styles from "./Conversation.module.css";

export const CONVERSATION_TITLE = "Conversation";
export const SIDECHAT = "Sidechat";
const DECISIONS_TITLE = "Routing";

/** The session's messages read, its id placed verbatim (MUST rule 25). */
export function sessionMessagesRoute(sessionId: string): string {
  return `/v1/sessions/${encodeURIComponent(sessionId)}/messages`;
}

/** The session's decisions read, its id placed verbatim (MUST rule 25). */
export function sessionDecisionsRoute(sessionId: string): string {
  return `/v1/sessions/${encodeURIComponent(sessionId)}/decisions`;
}

/** `wire`, scoped to one row's prefix: a `Wire` subtree travels like a `Leaf` (decision 42), the
 * same helper `app/models/panels.tsx::rowWire` already uses for `RegistryCard`. */
function rowWire(wire: Wire, prefix: string): Wire {
  return {
    route: wire.route,
    text: wire.text,
    byteLength: wire.byteLength,
    at: (path: string) => wire.at(`${prefix}${path}`),
  };
}

/** This message's own decisions, in the session's served order (never re-sorted). */
function decisionsForMessage(decisionsWire: Wire, messageSeq: string): Wire[] {
  const rows = rowsOf(decisionsWire, (row) => `.decisions[${row}].idx`);
  return rows
    .filter((row) => decisionsWire.at(`.decisions[${row}].message_seq`).text === messageSeq)
    .map((row) => rowWire(decisionsWire, `.decisions[${row}]`));
}

/** The session's permission decisions read, its id placed verbatim (MUST rule 25). */
export function sessionPermissionsRoute(sessionId: string): string {
  return `/v1/permissions?session=${encodeURIComponent(sessionId)}`;
}

/** This message's own permission decisions, placed by the served `message_seq` (never by time). */
function permissionsForMessage(permissionsWire: Wire, messageSeq: string): Wire[] {
  const rows = rowsOf(permissionsWire, (row) => `.permissions[${row}].permission_id`);
  return rows
    .filter((row) => leafIfSent(permissionsWire, `.permissions[${row}].message_seq`)?.text === messageSeq)
    .map((row) => rowWire(permissionsWire, `.permissions[${row}]`));
}

/**
 * The session's permission decisions (`GET /v1/permissions?session=`), read beside the messages and
 * read again on the same refetch signal. A decision whose card arrived or changed state is said
 * once, through the one polite region the conversation owns; a read that fails is shown above the
 * list and withholds nothing else (MUST rule 23).
 */
function usePermissions(sessionId: string, refresh: object | undefined, reread: object) {
  const origin = useContext(GatewayOriginContext);
  const route = sessionPermissionsRoute(sessionId);
  const [read, setRead] = useState<{ route: string; result: PanelResult } | undefined>(undefined);
  const [announced, setAnnounced] = useState("");
  // The state each decision was last seen in; `undefined` until the first read has landed.
  const seen = useRef<Map<string, string> | undefined>(undefined);

  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    void fetchPanel(route, origin, controller.signal).then((result) => {
      if (!live) return;
      if (result.state === "ok") {
        const first = seen.current === undefined;
        const states = seen.current ?? new Map<string, string>();
        const said: string[] = [];
        for (const row of rowsOf(result.wire, (index) => `.permissions[${index}].permission_id`)) {
          const permission = rowWire(result.wire, `.permissions[${row}]`);
          const id = permission.at(".permission_id").text;
          const state = permission.at(".state").text;
          const before = states.get(id);
          const arrived = state === "pending" ? before !== "pending" : before === undefined ? !first : before !== state;
          if (arrived) said.push(announcement(permission));
          states.set(id, state);
        }
        seen.current = states;
        if (said.length > 0) setAnnounced(said.join(". "));
      }
      setRead({ route, result });
    });
    return () => {
      live = false;
      controller.abort();
    };
  }, [route, origin, refresh, reread]);

  return { result: read?.route === route ? read.result : undefined, announced };
}

type Props = {
  sessionId: string;
  /** A refetch signal from the run this session is watching: a new object is one more read of
   * both this session's messages and its decisions (the Overview's own "SSE-for-change,
   * fetch-to-read" pattern, FR-75). */
  refresh?: object;
  /** Reports, after each render of the list, whether a message is live and the cursor its stream
   * opens at (the previous message's served `last_seq`, `undefined` for the first message). */
  onServed?: (served: Served) => void;
  /** Reports each settled read of the conversation panel (any result): the console's "reading" edge (ticket 216). */
  onSettled?: (result: PanelResult) => void;
  /** The task's served working directory, once its own read has landed (FR-154). */
  taskCwd?: Leaf;
  taskEntry?: Leaf;
  /** The task's served `created_at`, for the first message's "queued HH:MM". */
  taskQueued?: Leaf;
  /** The task's served state and pause reason, once its read has landed (FR-154). */
  taskState?: TaskState;
  /** Called once a permission answer is accepted: the console reads the task and the run again. */
  onAnswered?: () => void;
  /** Requests the stream's `acp.permission.request` frames named, placed before the read returns. */
  arrivals?: readonly Arrival[];
};

/** One `acp.permission.request` frame: the request's id and the exchange (`message_seq`) it names. */
export type Arrival = { permissionId: string; messageSeq: string };

export type SidechatProps = {
  session: string;
  open: boolean;
  onOpen: () => void;
  inFlight: boolean;
  /** The conversation's first read has not landed yet (ticket 216). */
  reading: boolean;
  /** The task this session belongs to, as its own read last said (FR-154). */
  task?: TaskState;
  onSent: (opened: Opened) => void;
};

/** Continue this conversation in place: one button, then the `Composer` on the same session id
 * (ticket 210). It renders below the conversation panel, outside its body, so it is present in
 * every panel state (ticket 216) and D-5's footer rule is untouched. */
export function Sidechat({ session, open, onOpen, inFlight, reading, task, onSent }: SidechatProps) {
  return (
    <div data-testid="sidechat" className={styles.sidechat}>
      {open ? (
        <Composer session={session} inFlight={inFlight} reading={reading} task={task} onSent={onSent} />
      ) : (
        <button type="button" className={styles.sidechatButton} onClick={onOpen}>
          {SIDECHAT}
        </button>
      )}
    </div>
  );
}

/** The served task state is `needs_human` with pause reason `uncertain`: read, never inferred (G-45). */
function pausedOnUncertainStop(task: TaskState | undefined): boolean {
  return task !== undefined && task.state.text === "needs_human" && task.pause.text === "uncertain";
}

export type Served = { live: boolean; afterSeq: Leaf | undefined; task: Leaf | undefined };

export function Conversation({ sessionId, refresh, onServed, onSettled, taskCwd, taskEntry, taskQueued, taskState, onAnswered, arrivals = [] }: Props) {
  // A fresh object per accepted answer: this component's own "read the decision again".
  const [answeredAt, setAnsweredAt] = useState<object>({});
  const permissions = usePermissions(sessionId, refresh, answeredAt);
  const answered = () => {
    setAnsweredAt({});
    onAnswered?.();
  };
  return (
    <>
      <p className={styles.hidden} role="status" aria-live="polite" data-testid="conversation-announcement">
        {permissions.announced}
      </p>
      <DecisionsFrame
        sessionId={sessionId}
        refresh={refresh}
        onServed={onServed}
        onSettled={onSettled}
        taskCwd={taskCwd}
        taskEntry={taskEntry}
        taskQueued={taskQueued}
        taskState={taskState}
        permissions={permissions.result}
        onAnswered={answered}
        arrivals={arrivals}
      />
    </>
  );
}

function DecisionsFrame({
  sessionId,
  refresh,
  onServed,
  onSettled,
  taskCwd,
  taskEntry,
  taskQueued,
  taskState,
  permissions,
  onAnswered,
  arrivals,
}: Props & { permissions: PanelResult | undefined; onAnswered: () => void; arrivals: readonly Arrival[] }) {
  return (
    <PanelFrame
      bare
      route={sessionDecisionsRoute(sessionId)}
      title={DECISIONS_TITLE}
      sourceLabel="Ledger: this session"
      refresh={refresh}
      onSettled={(result) => {
        // A decisions read that never reaches ok withholds the messages panel: nothing more will settle.
        if (result.state !== "ok") onSettled?.(result);
      }}
    >
      {(decisionsWire) => (
        <MessagesPanel
          sessionId={sessionId}
          decisionsWire={decisionsWire}
          refresh={refresh}
          onServed={onServed}
          onSettled={onSettled}
          taskCwd={taskCwd}
          taskEntry={taskEntry}
          taskQueued={taskQueued}
          taskState={taskState}
          permissions={permissions}
          onAnswered={onAnswered}
          arrivals={arrivals}
        />
      )}
    </PanelFrame>
  );
}

function MessagesPanel({
  sessionId,
  decisionsWire,
  refresh,
  onServed,
  onSettled,
  taskCwd,
  taskEntry,
  taskQueued,
  taskState,
  permissions,
  onAnswered,
  arrivals,
}: {
  sessionId: string;
  decisionsWire: Wire;
  refresh: object | undefined;
  onServed: ((served: Served) => void) | undefined;
  onSettled: ((result: PanelResult) => void) | undefined;
  taskCwd: Leaf | undefined;
  taskEntry: Leaf | undefined;
  taskQueued: Leaf | undefined;
  taskState: TaskState | undefined;
  permissions: PanelResult | undefined;
  onAnswered: () => void;
  arrivals: readonly Arrival[];
}) {
  // Captured while the decisions `PanelFrame` is still the nearest context, so it can be handed
  // whole to every `Exchange` and re-opened later around exactly the figures that belong to it.
  const decisionsPanel = usePanel();

  return (
    <PanelFrame
      route={sessionMessagesRoute(sessionId)}
      title={CONVERSATION_TITLE}
      sourceLabel="Ledger: this session"
      refresh={refresh}
      onSettled={onSettled}
    >
      {(messagesWire) => {
        const rows = rowsOf(messagesWire, (row) => `.messages[${row}].message_seq`);
        if (rows.length === 0) return null;
        const taskId = leafIfSent(messagesWire, ".task.task_id");
        // Presence checks only: each message's stream opens after the previous served `last_seq`.
        let previous: Leaf | undefined;
        let served: Served = { live: false, afterSeq: undefined, task: taskId };
        const exchanges = rows.map((row) => {
          const message = rowWire(messagesWire, `.messages[${row}]`);
          const messageSeq = message.at(".message_seq").text;
          const lastSeq = message.at(".last_seq");
          const live = lastSeq.kind === "null";
          const afterSeq = previous;
          if (live) served = { live: true, afterSeq, task: taskId };
          else previous = lastSeq;
          return (
            <Exchange
              key={messageSeq}
              message={message}
              decisions={decisionsForMessage(decisionsWire, messageSeq)}
              live={live}
              decisionsPanel={decisionsPanel}
              sessionId={sessionId}
              afterSeq={afterSeq}
              permissions={
                permissions?.state === "ok" ? permissionsForMessage(permissions.wire, messageSeq) : undefined
              }
              permissionsPanel={permissions?.state === "ok" ? panelOf(permissions) : undefined}
              onAnswered={onAnswered}
              arriving={arrivingFor(arrivals, messageSeq, permissions)}
              taskId={taskId}
              taskCwd={taskCwd}
              taskEntry={taskEntry}
              taskQueued={taskQueued}
              pausedUncertain={row === rows.at(-1) && pausedOnUncertainStop(taskState)}
            />
          );
        });
        return (
          <>
            <PermissionsRead permissions={permissions} />
            <ol className={styles.conversation} data-testid="conversation">
              {exchanges}
            </ol>
            <ServedReport served={served} onServed={onServed} />
            <Landed />
          </>
        );
      }}
    </PanelFrame>
  );
}

/** The ids of requests a frame placed on this exchange that the permissions read has not returned yet. */
function arrivingFor(arrivals: readonly Arrival[], messageSeq: string, permissions: PanelResult | undefined): string[] {
  const read =
    permissions?.state === "ok"
      ? rowsOf(permissions.wire, (row) => `.permissions[${row}].permission_id`).map(
          (row) => permissions.wire.at(`.permissions[${row}].permission_id`).text,
        )
      : [];
  return arrivals.filter((a) => a.messageSeq === messageSeq && !read.includes(a.permissionId)).map((a) => a.permissionId);
}

/** The context a card's figures cite: the permissions response, read through `fetchPanel`. */
function panelOf(result: Extract<PanelResult, { state: "ok" }>): PanelContextValue {
  return { response: result.response, panelId: "permissions", sourceLabel: "Ledger: this session" };
}

/** A permissions read that did not reach `ok`, quoted above the list; nothing else is withheld. */
function PermissionsRead({ permissions }: { permissions: PanelResult | undefined }) {
  if (permissions === undefined || permissions.state === "ok") return null;
  if (permissions.state === "unreachable") return <UnreachableState cause={permissions.cause} />;
  return <ErrorState status={permissions.response.status} error={permissions.error} message={permissions.message} />;
}

/** Reports what the list served once it has rendered (the `HandedOver` pattern, `ReplayPanel`). */
function ServedReport({
  served,
  onServed,
}: {
  served: Served;
  onServed: ((served: Served) => void) | undefined;
}) {
  useEffect(() => {
    onServed?.(served);
  });
  return null;
}

/** Lands on the exchange the URL's hash names: scrolled into view then focused, once per hash
 * value (a ref remembers the hash it already landed). A hash naming no exchange does nothing. */
function Landed() {
  const landed = useRef<string | null>(null);
  useEffect(() => {
    const hash = window.location.hash;
    if (hash === "" || landed.current === hash) return;
    let id = hash.slice(1);
    try {
      id = decodeURIComponent(id);
    } catch {
      return; // a malformed escape names no exchange
    }
    const element = document.getElementById(id);
    if (element === null || element.closest("[data-testid=conversation]") === null) return;
    landed.current = hash;
    element.scrollIntoView({ block: "start" });
    element.focus();
  });
  return null;
}
