import { createDataFrame, type DataFrame } from '@grafana/data';
import { readZoneProgress } from './zoneProgress';

const frame = (columns: Record<string, unknown[]>): DataFrame =>
  createDataFrame({ fields: Object.entries(columns).map(([name, values]) => ({ name, values })) });

// A real epoch in milliseconds, from the 2026-09-21 fixture.
const T = 1789986187389;
const MIN = 60_000;

describe('readZoneProgress', () => {
  test("each Zone's progress is its latest row, whatever order the rows arrive in", () => {
    const rows = frame({ zone: [8, 9, 8], progress: [64, 10, 40], time: [T + MIN, T, T] });
    expect(readZoneProgress([rows])).toEqual({
      '8': { progress: 64, time: T + MIN },
      '9': { progress: 10, time: T },
    });
  });

  test('without a time column, the last row for a Zone is taken as its latest', () => {
    expect(readZoneProgress([frame({ zone: ['a', 'a'], progress: [20, 30] })])).toEqual({ a: { progress: 30 } });
  });

  test('a frame without both columns, such as the Trail query, is someone else’s', () => {
    expect(readZoneProgress([frame({ time: [T], x: [1], y: [2], zone: [8] })])).toEqual({});
    expect(readZoneProgress([])).toEqual({});
  });

  test('progress read from several frames, as a query split by Zone returns it, is one answer', () => {
    const [front, back] = [
      frame({ zone: [1], progress: [5], time: [T] }),
      frame({ zone: [2], progress: [95], time: [T] }),
    ];
    expect(Object.keys(readZoneProgress([front, back]))).toEqual(['1', '2']);
  });

  test('column names can be overridden, and a blank override means the default', () => {
    const rows = frame({ partition: [3], percent: ['87.5'], time: [T] });
    expect(readZoneProgress([rows], { zone: 'partition', progress: 'percent', time: ' ' })).toEqual({
      '3': { progress: 87.5, time: T },
    });
  });

  test('a row with no Zone or no readable progress says nothing', () => {
    const rows = frame({ zone: [1, null, 2, 3], progress: [null, 50, 'n/a', 12], time: [T, T, T, T] });
    expect(readZoneProgress([rows])).toEqual({ '3': { progress: 12, time: T } });
  });

  test('progress is a percentage, held within 0 and 100', () => {
    const rows = frame({ zone: [1, 2], progress: [-3, 100.4] });
    expect(readZoneProgress([rows])).toEqual({ '1': { progress: 0 }, '2': { progress: 100 } });
  });
});
