import { createDataFrame, FieldType, type DataFrame } from '@grafana/data';
import { readTrails, type Trail } from './trailFrame';

type Columns = Record<string, unknown[]>;

const frame = (columns: Columns): DataFrame =>
  createDataFrame({
    fields: Object.entries(columns).map(([name, values]) => ({
      name,
      type: name === 'time' ? FieldType.time : FieldType.other,
      values,
    })),
  });

const trails = (frames: DataFrame[], columns = {}) => {
  const result = readTrails(frames, columns);
  if ('problem' in result) {
    throw new Error(result.problem);
  }
  return result.trails;
};

// A real epoch in milliseconds, from the 2026-09-21 fixture, and offsets from it.
const T = 1789986187389;
const SEC = 1000;
const MIN = 60 * SEC;
const at = (...offsets: number[]) => offsets.map((o) => T + o);
/** Each Trail's segments, as the x of each point. */
const xs = (result: Trail[]) => result.map((t) => [t.job, t.segments.map((s) => s.map((p) => p.x))]);

describe('readTrails', () => {
  test('a frame with no optional columns is one Trail of bare positions', () => {
    expect(trails([frame({ time: at(0, SEC), x: [0.5, 1.5], y: [-1, 2] })])).toEqual([
      {
        segments: [
          [
            { time: T, x: 0.5, y: -1 },
            { time: T + SEC, x: 1.5, y: 2 },
          ],
        ],
      },
    ]);
  });

  test('points are put in time order, since the mower delivers some late', () => {
    expect(xs(trails([frame({ time: at(2 * SEC, 0, SEC), x: [3, 1, 2], y: [0, 0, 0] })]))).toEqual([
      [undefined, [[1, 2, 3]]],
    ]);
  });

  test('optional columns are read when present', () => {
    const [trail] = trails([
      frame({
        time: at(0),
        x: [1],
        y: [2],
        theta: [1.039],
        job_id: ['job-1'],
        zone: [8],
        status: ['isRunning'],
        device_id: ['mower-1'],
      }),
    ]);
    expect(trail).toEqual({
      job: 'job-1',
      segments: [[{ time: T, x: 1, y: 2, heading: 1.039, zone: '8', status: 'isRunning', mower: 'mower-1' }]],
    });
  });

  test('a Job column splits the frame into one Trail per Job, earliest first', () => {
    const result = trails([
      frame({ time: at(4 * MIN, 0, 5 * MIN, MIN), x: [5, 1, 6, 2], y: [0, 0, 0, 0], job_id: ['b', 'a', 'b', 'a'] }),
    ]);
    expect(xs(result)).toEqual([
      ['a', [[1, 2]]],
      ['b', [[5, 6]]],
    ]);
  });

  test('positions outside any Job are kept, but never joined across a Job in between', () => {
    const result = trails([
      frame({ time: at(0, SEC, 2 * SEC, 3 * SEC), x: [0, 1, 2, 100], y: [0, 0, 0, 0], job_id: [null, 'a', 'a', ''] }),
    ]);
    expect(xs(result)).toEqual([
      [undefined, [[0], [100]]],
      ['a', [[1, 2]]],
    ]);
    expect(result.map((t) => t.outsideJob)).toEqual([true, undefined]);
  });

  // Grafana splits a SQL time series into a frame per value of a text column, such as status or Zone.
  test('a Job split across frames is segmented as if it were one frame', () => {
    const columns = { time: at(0, SEC, 2 * SEC), x: [0, 1, 2], y: [0, 0, 0], job_id: ['a', 'a', 'a'] };
    const whole = trails([frame(columns)]);
    const split = [0, 1, 2].map((i) =>
      frame(Object.fromEntries(Object.entries(columns).map(([name, values]) => [name, [values[i]]])))
    );
    expect(xs(trails(split))).toEqual(xs(whole));
    expect(xs(whole)).toEqual([['a', [[0, 1, 2]]]]);
  });

  test('a row without a position in another frame still breaks the line', () => {
    const result = trails([
      frame({ time: at(0, 2 * SEC), x: [0, 2], y: [0, 0], job_id: ['a', 'a'] }),
      frame({ time: at(SEC), x: [null], y: [null], job_id: ['a'] }),
    ]);
    expect(xs(result)).toEqual([['a', [[0], [2]]]]);
  });

  test('a silence longer than 15 minutes is a gap, not a straight line', () => {
    const result = trails([frame({ time: at(0, MIN, 17 * MIN, 18 * MIN), x: [0, 1, 2, 3], y: [0, 0, 0, 0] })]);
    expect(xs(result)).toEqual([
      [
        undefined,
        [
          [0, 1],
          [2, 3],
        ],
      ],
    ]);
  });

  test('a mower charging inside a Job, reporting every 5 minutes, stays one line', () => {
    const result = trails([frame({ time: at(0, 5 * MIN, 10 * MIN, 15 * MIN), x: [0, 1, 2, 3], y: [0, 0, 0, 0] })]);
    expect(xs(result)).toEqual([[undefined, [[0, 1, 2, 3]]]]);
  });

  test('a row without a position breaks the line', () => {
    const result = trails([frame({ time: at(0, SEC, 2 * SEC, 3 * SEC), x: [0, null, 2, 3], y: [0, 0, 0, 0] })]);
    expect(xs(result)).toEqual([[undefined, [[0], [2, 3]]]]);
  });

  test('the same Job from two mowers is two Trails, and neither breaks the other', () => {
    const result = trails([
      frame({
        time: at(0, SEC, 2 * SEC, 3 * SEC),
        x: [0, 10, 1, 11],
        y: [0, 0, 0, 0],
        job_id: ['a', 'a', 'a', 'a'],
        device_id: ['m1', 'm2', 'm1', 'm2'],
      }),
    ]);
    expect(xs(result)).toEqual([
      ['a', [[0, 1]]],
      ['a', [[10, 11]]],
    ]);
    expect(result.map((t) => t.segments[0][0].mower)).toEqual(['m1', 'm2']);
  });

  test('without a Job column, each frame is its own Trail', () => {
    const result = trails([frame({ time: at(MIN), x: [3], y: [0] }), frame({ time: at(0), x: [1], y: [0] })]);
    expect(xs(result)).toEqual([
      [undefined, [[1]]],
      [undefined, [[3]]],
    ]);
  });

  test('column names can be overridden', () => {
    const csv = frame({ time: at(0), postureX: ['-0.31'], postureY: ['-0.357'], postureTheta: ['1.039'] });
    expect(trails([csv], { x: 'postureX', y: 'postureY', heading: 'postureTheta' })).toEqual([
      { segments: [[{ time: T, x: -0.31, y: -0.357, heading: 1.039 }]] },
    ]);
  });

  test('times may arrive as text', () => {
    const [trail] = trails([frame({ time: ['2026-09-21T10:23:07.389Z', '1789986188447'], x: [0, 1], y: [0, 0] })]);
    expect(trail.segments.flat().map((p) => p.time)).toEqual([1789986187389, 1789986188447]);
  });

  test('epoch seconds, as a number or text, are read as seconds', () => {
    const [trail] = trails([frame({ time: [1789986187, '1789986188'], x: [0, 1], y: [0, 0] })]);
    expect(trail.segments.flat().map((p) => p.time)).toEqual([1789986187000, 1789986188000]);
  });

  // Jest pins TZ=UTC (jest.config.js) and a test cannot change it, so this states the contract rather
  // than reproducing a browser in another zone, where Date.parse reads zone-less text as local time.
  test('text times without a zone are UTC, as Grafana reads SQL; an explicit zone is kept', () => {
    const [trail] = trails([
      frame({
        time: ['2026-09-21 10:23:07', '2026-09-21T10:23:08.5', '2026-09-21T12:23:09+02:00'],
        x: [0, 1, 2],
        y: [0, 0, 0],
      }),
    ]);
    expect(trail.segments.flat().map((p) => new Date(p.time).toISOString())).toEqual([
      '2026-09-21T10:23:07.000Z',
      '2026-09-21T10:23:08.500Z',
      '2026-09-21T10:23:09.000Z',
    ]);
  });

  test('rows without a usable position are skipped, missing optional values are left out', () => {
    const [trail] = trails([
      frame({ time: [...at(0, SEC, 2 * SEC), null], x: [1, null, 3, 4], y: [1, 2, 'n/a', 4], theta: [null, 0, 0, 0] }),
    ]);
    expect(trail.segments).toEqual([[{ time: T, x: 1, y: 1 }]]);
  });

  test.each([
    ['no data at all', []],
    ['a query that returned no rows', [frame({ time: [], x: [], y: [] })]],
    ['a query that returned nothing usable', [frame({ time: at(0), x: [null], y: [null] })]],
    ['an empty frame without fields', [createDataFrame({ fields: [] })]],
  ])('an empty range (%s) has no Trails', (_, frames) => {
    expect(trails(frames)).toEqual([]);
  });

  test.each([
    ['x', { time: at(0, SEC), x: ['-0,31', '1,0'], y: [0, 0] }, '-0,31'],
    ['time', { time: ['21.09.2026 10:23:07'], x: [0], y: [0] }, '21.09.2026 10:23:07'],
    ['time', { time: ['2026-09-21T10:00:00+02'], x: [0], y: [0] }, '2026-09-21T10:00:00+02'],
  ])('a "%s" column with nothing readable says so, with an example', (column, columns, example) => {
    expect(readTrails([frame(columns)])).toEqual({
      problem: expect.stringContaining(`"${column}" column has no value the Trail can read, such as "${example}"`),
    });
  });

  test('frames without the Trail columns, such as another query, are ignored', () => {
    const result = trails([frame({ zone: [1], progress: [50] }), frame({ time: at(0), x: [1], y: [1] })]);
    expect(result).toHaveLength(1);
  });

  test('an empty Trail query beside another query with rows is an empty range, not a problem', () => {
    const zoneProgress = frame({ zone: [1, 2], progress: [50, 10], time: at(0, SEC) });
    expect(readTrails([frame({ time: [], x: [], y: [] }), zoneProgress])).toEqual({ trails: [] });
  });

  test('the problem names what the closest frame lacks, not whichever came first', () => {
    const zoneProgress = frame({ zone: [1], progress: [50], time: at(0) });
    expect(readTrails([zoneProgress, frame({ time: at(0), x: [1], northing: [1] })])).toEqual({
      problem: expect.stringMatching(/^No "y" column for/),
    });
  });

  test('missing required columns are named, with where to fix them', () => {
    expect(readTrails([frame({ time: at(0), postureX: [1], postureY: [1] })], {})).toEqual({
      problem: expect.stringMatching(/"x" and "y".*Trail columns/),
    });
    expect(readTrails([frame({ time: at(0), x: [1] })], { y: 'northing' })).toEqual({
      problem: expect.stringMatching(/"northing"/),
    });
  });
});
