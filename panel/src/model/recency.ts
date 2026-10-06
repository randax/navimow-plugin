export type Recency = { stale: false } | { stale: true; lastSeen: string };

// A mower reports every 2 s while out and every 5 min while docked (#6), and a docked heartbeat may
// alternate with a placeholder the collector drops (#4), so a healthy mower can go 10 min between
// stored positions. 15 min tolerates that plus delivery lag without flickering, and is still far
// short of the hour-old position, shown as current, that this exists to prevent.
export const STALE_AFTER_MS = 15 * 60_000;

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

/** A length of time in the largest unit that says something: minutes, then hours, then days. */
export const duration = (ms: number): string =>
  ms < HOUR ? `${Math.floor(ms / MIN)} min` : ms < 2 * DAY ? `${Math.floor(ms / HOUR)} h` : `${Math.floor(ms / DAY)} d`;

/**
 * Whether a position from `time` still describes the mower at `now`. `now` is the wall clock, which
 * the panel re-reads every minute, not the fetch time or the end of the dashboard's range: with
 * refresh off, a mower that stops reporting must still go stale, and in a range that ended in the
 * past, the last position is history and must not look current.
 */
export function recency(time: number, now: number): Recency {
  const age = now - time;
  if (age < STALE_AFTER_MS) {
    return { stale: false };
  }
  return { stale: true, lastSeen: `Last seen ${duration(age)} ago` };
}
