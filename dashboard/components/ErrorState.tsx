/**
 * A non-2xx that is neither a 404 nor an unreachable gateway (MUST rule 23).
 *
 * A 400 with its `LimitError`, a 500 with its `StorageError`, a 502 that carried no transport
 * header: the gateway answered, and what it said is the news. The status, the `error` class and
 * the `message` are quoted verbatim (`D-7`) and the one action is to read it again, because
 * nothing here writes (`D-8`). The console's composer shows the control app's refusals in the
 * same strip, answered by the control app and with no `Refetch`: its one action is `Send` again.
 */
import styles from "./ErrorState.module.css";

type Props = {
  /** The HTTP status as text: this tree holds no numbers (decision 32). */
  status: string;
  /** The gateway's error class name, verbatim. */
  error: string;
  /** The gateway's message, verbatim. */
  message: string;
  /** Absent where reading again is not the action, as under the console's composer. */
  onRefetch?: () => void;
  /** Who answered: the read gateway, or the control app a write went to. */
  answeredBy?: string;
};

export function ErrorState({ status, error, message, onRefetch, answeredBy = "gateway" }: Props) {
  return (
    <div className={styles.failed} role="alert">
      <p className={styles.body}>
        The {answeredBy} answered <code className={styles.status}>{status}</code>{" "}
        <code className={styles.error}>{error}</code>:
      </p>
      <p className={styles.message}>{message}</p>
      {onRefetch === undefined ? null : (
        <button type="button" className={styles.refetch} onClick={onRefetch}>
          Refetch
        </button>
      )}
    </div>
  );
}
