"use client";

/**
 * Decision 30a's announcement batching, in the one place it lives (FR-59, FR-85).
 *
 * A frame is shown when it arrives; only what is *said* about it batches. `arrive` hands over the
 * frames that just landed, and `announced` is one sentence per `ANNOUNCE_INTERVAL_MS` naming how
 * many arrived and the newest one's name as the frame spelled it (`announcementFor`). The
 * session stream's panel and the architecture diagram both say it this way, so it is written
 * once: FR-85 reuses this batching rather than reimplementing it.
 *
 * Trailing only: the first frame of a burst arms the interval rather than announcing at once, so
 * N frames inside one interval are one announcement and not two. An interval that finds nothing
 * waiting says nothing and stops, so the next arrival starts a fresh one.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { ANNOUNCE_INTERVAL_MS, type StreamFrame, announcementFor } from "../lib/stream";

export type Announcement = {
  /** The latest batched sentence, or `""` before the first interval has closed. */
  announced: string;
  /** Hand over the frames that just landed. Safe to call from a callback or an effect. */
  arrive: (frames: readonly StreamFrame[]) => void;
};

export function useAnnouncement(): Announcement {
  const [announced, setAnnounced] = useState("");
  const pending = useRef<StreamFrame[]>([]);
  const cooling = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const announce = useCallback(function run() {
    const batch = pending.current;
    pending.current = [];
    if (batch.length === 0) {
      cooling.current = undefined;
      return;
    }
    setAnnounced(announcementFor(batch));
    cooling.current = setTimeout(run, ANNOUNCE_INTERVAL_MS);
  }, []);

  const arrive = useCallback(
    (frames: readonly StreamFrame[]) => {
      if (frames.length === 0) return;
      pending.current.push(...frames);
      if (cooling.current === undefined) {
        cooling.current = setTimeout(announce, ANNOUNCE_INTERVAL_MS);
      }
    },
    [announce],
  );

  useEffect(() => {
    return () => {
      if (cooling.current !== undefined) clearTimeout(cooling.current);
    };
  }, []);

  return { announced, arrive };
}
