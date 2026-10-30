/** Removes the run's temporary `HARNESSED_HOME`; a file the gateway still holds is left behind. */
import { rmSync } from "node:fs";
import { dirname } from "node:path";

import { HOME_VARIABLE } from "./harness";

export default function globalTeardown(): void {
  const home = process.env[HOME_VARIABLE];
  if (!home) return;
  try {
    rmSync(dirname(home), { recursive: true, force: true, maxRetries: 5 });
  } catch {
    // Windows keeps a served SQLite file locked until the gateway exits; the OS temp dir reaps it.
  }
}
