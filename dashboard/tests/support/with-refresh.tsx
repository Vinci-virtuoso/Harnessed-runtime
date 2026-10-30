import { type ReactNode, useMemo, useState } from "react";

import { RefreshContext } from "../../components/PanelFrame";

/**
 * The shell's one `Refresh` without the rest of the shell (ticket 211): a bare `PanelFrame` under
 * it re-reads on the button exactly as it does under `AppShell`, and no gateway pill polls beside it.
 */
export function WithRefresh({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<object>({});
  const value = useMemo(() => ({ token, refetch: () => setToken({}) }), [token]);
  return (
    <RefreshContext.Provider value={value}>
      <header>
        <button type="button" onClick={value.refetch}>
          Refresh
        </button>
      </header>
      <main>{children}</main>
    </RefreshContext.Provider>
  );
}
