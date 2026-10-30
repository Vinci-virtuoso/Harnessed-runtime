/**
 * The surface the gateway answered 404 for (`D-3`).
 *
 * An empty table would say "this exists and holds nothing", which is a different fact and the one
 * `D-3` exists to stop. So this state quotes the gateway's own `error` class and `message`
 * verbatim (decision 24's error contract, `D-7`), says the status out loud, and is an alert on
 * first render. Where a route's 404 means something the message does not spell - the episodes
 * route is decision 24's example - `absentText` carries that sentence from `lib/null-reasons.ts`.
 */
import styles from "./AbsentState.module.css";

type Props = {
  /** The gateway's error class name, verbatim. */
  error: string;
  /** The gateway's message, verbatim. */
  message: string;
  /** What a 404 means on this route, where it means more than "not found". */
  absentText?: string;
};

export function AbsentState({ error, message, absentText }: Props) {
  return (
    <div className={styles.absent} role="alert">
      <p className={styles.body}>
        Not found. The gateway answered 404 <code className={styles.error}>{error}</code>:
      </p>
      <p className={styles.message}>{message}</p>
      {absentText ? <p className={styles.body}>{absentText}</p> : null}
    </div>
  );
}
