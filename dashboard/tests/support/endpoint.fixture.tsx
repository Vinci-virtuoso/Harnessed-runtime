/** Seeded: components that draw a route in a `title`. Both scans must fail on them (MUST rule 12). */
export function Leaky({ leaf }: { leaf?: { route: string; path: string } }) {
  return (
    <span title="GET /v1/runs">
      runs
      {leaf === undefined ? null : <i title={`${leaf.route} -> ${leaf.path}`}>1</i>}
    </span>
  );
}
