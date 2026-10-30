import type { RawResponse } from "../../lib/fetch-panel";
import type { Leaf } from "../../lib/wire";
import { ResponseSource } from "../PanelFrame";
import { Value } from "../Value";
import styles from "./diagram.module.css";

type LabelsProps = {
  keys: readonly string[];
  labels: Record<string, Leaf>;
  sources: Record<string, RawResponse>;
};

/** One region's labels: each handed `Leaf` under its key, through `Value`, cited to its response. */
export function Labels({ keys, labels, sources }: LabelsProps) {
  const handed = keys.filter((key) => labels[key] !== undefined);
  if (handed.length === 0) return null;
  return (
    <dl className={styles.labels}>
      {handed.map((key) => (
        <div key={key} className={styles.label}>
          <dt>{key}</dt>
          <dd>
            <Label leaf={labels[key]} source={sources[labels[key].route]} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** A label's figure, under the response its tile read where the diagram was handed it. */
function Label({ leaf, source }: { leaf: Leaf; source: RawResponse | undefined }) {
  if (source === undefined) return <Value leaf={leaf} />;
  return (
    <ResponseSource response={source} method="GET" sourceLabel="Ledger: all time">
      <Value leaf={leaf} />
    </ResponseSource>
  );
}
