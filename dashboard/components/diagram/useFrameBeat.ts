import { useState } from "react";

import { now } from "../../lib/clock";
import type { StreamFrame } from "../../lib/stream";
import { FLASH } from "./flash";
import { useReducedMotion } from "./useReducedMotion";

/**
 * The frame-by-frame highlight both diagrams share (FR-85): the first frame not yet shown is the
 * `current` one, its highlight class alternates between two names so each restarts its animation,
 * and `ended` (the hidden beat's `animationend`) marks it shown so the next lights. Under reduced
 * motion there is no `current`: nothing is lit and no beat runs. `lastFrame` and `lastAt` are the
 * newest arrival and when the browser received it, for the status line.
 */
export function useFrameBeat(frames: readonly StreamFrame[]): {
  current: StreamFrame | undefined;
  flash: string;
  ended: (frame: StreamFrame) => void;
  lastFrame: StreamFrame | undefined;
  lastAt: Date | undefined;
} {
  const reduced = useReducedMotion();
  /**
   * The frames whose highlight has run its course, held by identity rather than by `seq`, so a
   * new session's `seq` 1 is a new frame and not one already shown.
   */
  const [shown, setShown] = useState<ReadonlySet<StreamFrame>>(() => new Set());
  const [flip, setFlip] = useState(false);

  // "Adjusting state during render" (react.dev), not an effect: the newest frame's own arrival
  // time is read once per new frame identity, so the highlight timing stays the only effect.
  const lastFrame = frames.at(-1);
  const [trackedFrame, setTrackedFrame] = useState<StreamFrame | undefined>(undefined);
  const [lastAt, setLastAt] = useState<Date | undefined>(undefined);
  if (lastFrame !== trackedFrame) {
    setTrackedFrame(lastFrame);
    setLastAt(lastFrame === undefined ? undefined : now());
  }

  const current = reduced ? undefined : frames.find((frame) => !shown.has(frame));
  const flash = flip ? FLASH[1] : FLASH[0];
  const ended = (frame: StreamFrame) => {
    setShown((done) => new Set(done).add(frame));
    setFlip((was) => !was);
  };
  return { current, flash, ended, lastFrame, lastAt };
}
