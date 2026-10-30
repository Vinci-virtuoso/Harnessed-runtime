/**
 * A collection the gateway answered 200 with and that held zero rows (`D-3`, `D-9`).
 *
 * It is deliberately not the absent state: the surface exists, it was read, and it has nothing in
 * it yet. So it reads "No <things> recorded yet." - the noun taken from the panel's own title,
 * lowercased, never the route that answered (`D-9` retires "The ledger returned 0 rows at GET
 * ...") - plus, where the panel supplies a more specific sentence, that sentence too, and the CLI
 * command that would create some. Plain text, no alert and no illustration: nothing here went wrong.
 */
import styles from "./EmptyState.module.css";

type Props = {
  /** The panel's own title, so the headline names what was looked for without naming a route. */
  title: string;
  /** A more specific sentence, where zero rows mean more than "nothing here yet". */
  emptyText?: string;
  /** The CLI command that would put rows here. */
  cliTwin?: string;
};

export function EmptyState({ title, emptyText, cliTwin }: Props) {
  return (
    <div className={styles.empty}>
      <p className={styles.body}>No {title.toLowerCase()} recorded yet.</p>
      {emptyText ? <p className={styles.body}>{emptyText}</p> : null}
      {cliTwin ? (
        <p className={styles.body}>
          Create some with <code className={styles.command}>{cliTwin}</code>.
        </p>
      ) : null}
    </div>
  );
}
