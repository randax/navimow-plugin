import { recency } from './recency';

const NOW = Date.UTC(2026, 9, 4, 12);
const ago = (ms: number) => recency(NOW - ms, NOW);
const MIN = 60_000;
const HOUR = 60 * MIN;

describe('recency', () => {
  test.each([
    ['just now', 0],
    ['between two docked heartbeats', 5 * MIN],
    ['a missed heartbeat later', 14 * MIN + 59_000],
  ])('a position from %s is current', (_, age) => {
    expect(ago(age)).toEqual({ stale: false });
  });

  test('a mower clock running ahead of the dashboard is not stale', () => {
    expect(ago(-2 * MIN)).toEqual({ stale: false });
  });

  test.each([
    ['Last seen 15 min ago', 15 * MIN],
    ['Last seen 59 min ago', 59 * MIN + 59_000],
    ['Last seen 3 h ago', 3 * HOUR + 20 * MIN],
    ['Last seen 47 h ago', 47 * HOUR],
    ['Last seen 13 d ago', 13 * 24 * HOUR + 5 * HOUR],
  ])('a stale position says "%s"', (lastSeen, age) => {
    expect(ago(age)).toEqual({ stale: true, lastSeen });
  });
});
