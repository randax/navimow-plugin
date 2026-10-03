export type Recency = { stale: false } | { stale: true; lastSeen: string };

// A mower reports every 2 s while out and every 5 min while docked (#6), and a docked heartbeat may
// alternate with a placeholder the collector drops (#4), so a healthy mower can go 10 min between
// stored positions. 15 min tolerates that plus delivery lag without flickering, and is still far
// short of the hour-old position, shown as current, that this exists to prevent.
const STALE_AFTER_MS = 15 * 60_000;

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

/**
 * Whether a position from `time` still describes the mower at `now`. `now` is the wall clock rather
 * than the end of the dashboard's range: in a range that ended in the past, the last position is
 * history, and must not look current just because the mower was busy when the range closed.
 */
export function recency(time: number, now: number): Recency {
  const age = now - time;
  if (age < STALE_AFTER_MS) {
    return { stale: false };
  }
  const amount =
    age < HOUR
      ? `${Math.floor(age / MIN)} min`
      : age < 2 * DAY
        ? `${Math.floor(age / HOUR)} h`
        : `${Math.floor(age / DAY)} d`;
  return { stale: true, lastSeen: `Last seen ${amount} ago` };
}
