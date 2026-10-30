"use client";

/**
 * The panel every screen is built from: one request, one state, and the response kept whole.
 *
 * `PanelFrame` owns exactly one gateway read (decision 34), in the browser, with no cache and
 * nothing remembered between loads (`D-6`). It renders exactly one of `loading`, `ok`, `empty`,
 * `absent`, `error` and `unreachable`, wearing `stale` over whichever of them has a response, and
 * it never shows a previous answer beside a new one: a refetch replaces the state, it does not
 * merge with it.
 *
 * **It dates itself and then only ages.** The read happens once, on mount and on every navigation
 * or `Refetch`; after that a timer re-reads `lib/clock.ts` once a minute and re-renders, and no
 * data route is ever requested again on its own (`D-5`, `D-6`; only `GatewayStatusPill` polls).
 * The one other read is a caller's `refresh`: a new token is a refetch signal from outside the
 * panel, which is how the Overview's watched stream asks every panel to read again (FR-75). The
 * token is a signal and never a figure: the panel still renders only the response it fetched.
 * Past the five minute mark the footer wears a `StaleBadge` that keeps counting, while the panel's
 * live region says the crossing exactly once: the sentence it announces is frozen at the moment it
 * crossed, so a screen reader hears "stale" once rather than once a minute.
 *
 * Its children are a function of the parsed `Wire`, so a figure can only be a `Leaf` of the
 * response this panel fetched (MUST rule 21). Returning `null` from that function is how a child
 * says "the collection the gateway sent is empty", which is the one thing `PanelFrame` cannot
 * know for itself: the shape belongs to the route. The raw response and the panel's id go to the
 * children through a context, so `SourceChip` and `ProvenanceDrawer` (ticket 102) can show where
 * a figure came from without the response being threaded through every component between.
 */
import {
  type ReactNode,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useState,
} from "react";

import { STALE_TICK_MS, isStale, minutesAgo, now } from "../lib/clock";
import { GATEWAY_ORIGIN, fetchPanel, type PanelResult, type RawResponse } from "../lib/fetch-panel";
import { absentTextFor, emptyTextFor } from "../lib/null-reasons";
import type { Wire } from "../lib/wire";

import { AbsentState } from "./AbsentState";
import { EmptyState } from "./EmptyState";
import { ErrorState } from "./ErrorState";
import { StaleBadge } from "./StaleBadge";
import { UnreachableState } from "./UnreachableState";
import styles from "./PanelFrame.module.css";

/**
 * The origin the panels under it read the gateway at: the relative `/v1`, because the process
 * that served the page is the one the gateway is mounted in (decision 43). `AppShell` provides
 * the same value, so the drawer and the `curl` lines name `/v1/...` wherever a panel is rendered.
 */
export const GatewayOriginContext = createContext<string>(GATEWAY_ORIGIN);

/**
 * The shell's one `Refresh` (ticket 211): `token` is a fresh object per press, which every
 * `PanelFrame` and every bare read under the shell folds into its own refetch signal; `refetch`
 * is what the shell's button calls. Outside a shell the token never changes and `refetch` is inert.
 */
export type RefreshContextValue = { token: object; refetch: () => void };

const NO_REFRESH: RefreshContextValue = { token: {}, refetch: () => undefined };

export const RefreshContext = createContext<RefreshContextValue>(NO_REFRESH);

/**
 * What a panel gives the figures inside it: the response they came from, who to ask (`D-4`'s
 * source label), and who to credit for a write. `method` is absent for every gateway read, which
 * is a `GET`; only `ResponseSource` sets it, and nothing renders it (`D-9`).
 */
export type PanelContextValue = {
  response: RawResponse;
  panelId: string;
  sourceLabel: string;
  method?: string;
};

const PanelContext = createContext<PanelContextValue | undefined>(undefined);

/**
 * The source of figures read out of a response no `PanelFrame` fetched: the control app's `202`,
 * which is the one figure on the dashboard that comes from a write (FR-81, design.md console
 * state 2). The figures under it cite that response and open the same drawer a fetched panel's
 * figures do, under the source label handed here (`D-4`).
 */
export function ResponseSource({
  response,
  method,
  sourceLabel,
  children,
}: {
  response: RawResponse;
  method: string;
  sourceLabel: string;
  children: ReactNode;
}) {
  const panelId = useId();
  return (
    <PanelContext.Provider value={{ response, panelId, sourceLabel, method }}>
      {children}
    </PanelContext.Provider>
  );
}

/** The response the surrounding panel rendered. Outside a `PanelFrame` there is no answer. */
export function usePanel(): PanelContextValue {
  const found = useContext(PanelContext);
  if (found === undefined) {
    throw new Error("a figure's source is the response its PanelFrame fetched; there is none here");
  }
  return found;
}

/** The list skeleton of design.md: three rows, so a loading panel is the height of a small table. */
const SKELETON_ROWS = ["first", "second", "third"];

/** One answer, and the read it answers: a panel's state is the pair, never the answer alone. */
type Answered = {
  route: string;
  origin: string;
  token: object;
  refresh: object | undefined;
  result: PanelResult;
};

/**
 * A crossing of the five minute mark, paired with the response that crossed it: the announcement
 * fires once (`D-5`), while the footer's own "Updated N min ago" keeps counting on every render
 * regardless (below). A new response is a new object, so `Refetch` clears the crossing for free.
 */
type Crossed = { response: RawResponse; at: Date };

type Props = {
  /** The gateway route this panel owns, as the route table spells it. */
  route: string;
  title: string;
  /** The plain label of who or what produced this panel's figures (`D-4`'s source-label vocabulary). */
  sourceLabel: string;
  /** Rendered over the parsed response; `null` means the gateway sent zero rows. */
  children: (wire: Wire) => ReactNode;
  /** What zero rows mean on this route, beyond "No <title> recorded yet." */
  emptyText?: string;
  /** The CLI command that would create rows, and the `--json` twin the drawer shows (FR-51). */
  cliTwin?: string;
  /**
   * A panel that draws nothing at all, heading included, when its children return `null` from an
   * answered read (`WorkSuiteRow`'s "the heading is not drawn when no suite has `kind: work`").
   * Every other state still renders as usual.
   */
  quiet?: boolean;
  /**
   * A refetch signal from outside the panel (FR-75): a new object is one more read, exactly as a
   * `Refetch` press is. Absent, the panel reads on mount and on `Refetch` only.
   */
  refresh?: object;
  /**
   * Called with every settled read, `ok` included, so a caller that has no figure of its own to
   * render here can still learn how this panel's one request came out (FR-92's replay-failure
   * edge: nothing else tells `ConsolePanel` that `ReplayPanel`'s read never reached `ok`).
   */
  onSettled?: (result: PanelResult) => void;
  /** How many skeleton rows a first read shows; design.md's list skeleton is three, `NeedsYouList`'s two. */
  skeletonRows?: number;
  /**
   * The `StatGroup` look (FR-163): one card holding a small sentence-case title and the figures,
   * with no header rule and no footer. The "Updated" line belongs to the page and the Refresh to
   * the shell, so a card draws a `StaleBadge` only once its read has aged past five minutes: an
   * old figure must never pass for a fresh one (`D-5`, `D-6`).
   */
  card?: boolean;
  /**
   * Draws no footer of its own, for a frame that wraps another panel's frame and so would repeat
   * the inner one's "Updated N min ago" (the console's decisions frame around its messages).
   */
  bare?: boolean;
};

export function PanelFrame({
  route,
  title,
  sourceLabel,
  children,
  emptyText,
  cliTwin,
  quiet = false,
  refresh: callerRefresh,
  onSettled,
  skeletonRows = SKELETON_ROWS.length,
  card = false,
  bare = false,
}: Props) {
  const origin = useContext(GatewayOriginContext);
  const { token: shellToken } = useContext(RefreshContext);
  // A fresh object when the caller's signal or the shell's Refresh changes: one more read either way.
  const refresh = useMemo(() => ({ callerRefresh, shellToken }), [callerRefresh, shellToken]);
  const panelId = useId();
  const headingId = `${panelId}-title`;
  // A fresh token is what a refetch is: a new read, not an increment of an old one.
  const [attempt, setAttempt] = useState<object>({});
  const refetch = useCallback(() => setAttempt({}), []);
  const [answered, setAnswered] = useState<Answered | undefined>(undefined);

  /*
   * The answer carries the read it belongs to, so "a read is in flight" is read off the state
   * rather than written into it: a new route, origin or attempt makes the previous answer stop
   * being this panel's answer, and the panel is loading again without anything being cleared.
   * That is also `D-6` at the smallest scale - a stale answer can never be rendered as a fresh
   * one, because it no longer matches the read.
   */
  const kept =
    answered !== undefined &&
    answered.route === route &&
    answered.origin === origin &&
    answered.token === attempt;
  const result = kept ? answered.result : undefined;
  // A `refresh` re-read keeps the previous answer on screen, marked busy, with its own Updated
  // time (D-6: nothing stale is shown as fresh); the skeleton is for a first read, a route change
  // and the `Refresh` button.
  const fresh = kept && answered.refresh === refresh;
  const settled = fresh ? result : undefined;

  useEffect(() => {
    if (settled !== undefined) onSettled?.(settled);
  }, [settled, onSettled]);

  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    void fetchPanel(route, origin, controller.signal).then((next) => {
      if (live) setAnswered({ route, origin, token: attempt, refresh, result: next });
    });
    return () => {
      live = false;
      controller.abort();
    };
  }, [route, origin, attempt, refresh]);

  const response = result === undefined || result.state === "unreachable" ? undefined : result.response;

  // A fresh object, never a counter (decision 32 bans `+`/`-` here): its only job is to force a
  // re-render once a minute, so the footer and every `Value`'s tooltip re-read `now()` fresh. A
  // toggled boolean would risk an even number of batched ticks cancelling back to its own value,
  // which is exactly the miss a fresh object by identity can never make (`Object.is` never agrees).
  const [, setTick] = useState<object>({});
  const [crossed, setCrossed] = useState<Crossed | undefined>(undefined);

  /*
   * The only timer a panel owns, and it asks nothing of the gateway: every minute it re-reads the
   * dashboard's clock, so "Updated N min ago" keeps counting, and records the five-minute crossing
   * once against the response that aged, for the live region below.
   */
  useEffect(() => {
    if (response === undefined) return undefined;
    const dated = response;
    const interval = setInterval(() => {
      const at = now();
      setTick({});
      if (!isStale(dated.fetchedAt, at)) return;
      setCrossed((previous) => (previous !== undefined && previous.response === dated ? previous : { response: dated, at }));
    }, STALE_TICK_MS);
    return () => clearInterval(interval);
  }, [response]);

  const at = now();
  if (quiet && result?.state === "ok" && children(result.wire) === null) return null;
  const stale = response !== undefined && isStale(response.fetchedAt, at);
  const announcement =
    response !== undefined && crossed !== undefined && crossed.response === response
      ? `Updated ${minutesAgo(crossed.response.fetchedAt, crossed.at)}`
      : "";

  return (
    <section className={card ? styles.card : styles.panel} aria-labelledby={headingId} aria-busy={!fresh}>
      <header className={card ? styles.cardHeader : styles.header}>
        <h2 id={headingId} className={card ? styles.cardTitle : styles.title}>
          {title}
        </h2>
      </header>
      <p
        className={styles.announcement}
        role="status"
        aria-live="polite"
        data-testid="panel-announcement"
      >
        {announcement}
      </p>
      <div className={card ? styles.cardBody : styles.body}>
        {response === undefined ? (
          body(result, { route, title, emptyText, cliTwin, children, refetch, skeletonRows })
        ) : (
          <PanelContext.Provider value={{ response, panelId, sourceLabel }}>
            {body(result, { route, title, emptyText, cliTwin, children, refetch, skeletonRows })}
          </PanelContext.Provider>
        )}
      </div>
      {response === undefined || bare || (card && !stale) ? null : (
        <footer className={styles.footer}>
          <StaleBadge label={`Updated ${minutesAgo(response.fetchedAt, at)}`} warn={stale} />
        </footer>
      )}
    </section>
  );
}

type BodyProps = {
  route: string;
  title: string;
  emptyText: string | undefined;
  cliTwin: string | undefined;
  children: (wire: Wire) => ReactNode;
  refetch: () => void;
  skeletonRows: number;
};

/** Exactly one state. The order is the order a response is read in, not a preference. */
function body(result: PanelResult | undefined, props: BodyProps): ReactNode {
  if (result === undefined) {
    return (
      <div className={styles.skeleton}>
        {SKELETON_ROWS.slice(0, props.skeletonRows).map((row) => (
          <div key={row} className={styles.skeletonRow} data-testid="skeleton-row" />
        ))}
      </div>
    );
  }

  if (result.state === "unreachable") {
    return <UnreachableState cause={result.cause} />;
  }

  if (result.state === "absent") {
    return (
      <AbsentState
        error={result.error}
        message={result.message}
        absentText={absentTextFor(props.route)}
      />
    );
  }

  if (result.state === "error") {
    return (
      <ErrorState
        status={result.response.status}
        error={result.error}
        message={result.message}
        onRefetch={props.refetch}
      />
    );
  }

  const rendered = props.children(result.wire);
  if (rendered === null || rendered === undefined) {
    return (
      <EmptyState
        title={props.title}
        emptyText={props.emptyText ?? emptyTextFor(props.route)}
        cliTwin={props.cliTwin}
      />
    );
  }
  return rendered;
}
