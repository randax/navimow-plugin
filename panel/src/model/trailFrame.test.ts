import { createDataFrame, FieldType, type DataFrame } from '@grafana/data';
import { readTrails } from './trailFrame';

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

describe('readTrails', () => {
  test('a frame with no optional columns is one Trail of bare positions', () => {
    expect(trails([frame({ time: [1000, 2000], x: [0.5, 1.5], y: [-1, 2] })])).toEqual([
      {
        points: [
          { time: 1000, x: 0.5, y: -1 },
          { time: 2000, x: 1.5, y: 2 },
        ],
      },
    ]);
  });

  test('points are put in time order, since the mower delivers some late', () => {
    const [trail] = trails([frame({ time: [3000, 1000, 2000], x: [3, 1, 2], y: [0, 0, 0] })]);
    expect(trail.points.map((p) => p.x)).toEqual([1, 2, 3]);
  });

  test('optional columns are read when present', () => {
    const [trail] = trails([
      frame({
        time: [1000],
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
      points: [{ time: 1000, x: 1, y: 2, heading: 1.039, zone: '8', status: 'isRunning', mower: 'mower-1' }],
    });
  });

  test('a Job column splits the frame into one Trail per Job, earliest first', () => {
    const result = trails([
      frame({ time: [5000, 1000, 6000, 2000], x: [5, 1, 6, 2], y: [0, 0, 0, 0], job_id: ['b', 'a', 'b', 'a'] }),
    ]);
    expect(result.map((t) => [t.job, t.points.map((p) => p.x)])).toEqual([
      ['a', [1, 2]],
      ['b', [5, 6]],
    ]);
  });

  // Positions outside any Job, such as at the dock, still say where the mower is, so they are kept.
  test('rows with no Job are kept together as one Trail of their own', () => {
    const result = trails([
      frame({ time: [1000, 2000, 3000, 4000], x: [1, 2, 3, 4], y: [0, 0, 0, 0], job_id: ['a', null, 'a', ''] }),
    ]);
    expect(result.map((t) => [t.job, t.points.map((p) => p.x)])).toEqual([
      ['a', [1, 3]],
      [undefined, [2, 4]],
    ]);
  });

  test('without a Job column, each frame is its own Trail', () => {
    const result = trails([frame({ time: [3000], x: [3], y: [0] }), frame({ time: [1000], x: [1], y: [0] })]);
    expect(result.map((t) => t.points[0].x)).toEqual([1, 3]);
  });

  test('column names can be overridden', () => {
    const csv = frame({ time: [1000], postureX: ['-0.31'], postureY: ['-0.357'], postureTheta: ['1.039'] });
    expect(trails([csv], { x: 'postureX', y: 'postureY', heading: 'postureTheta' })).toEqual([
      { points: [{ time: 1000, x: -0.31, y: -0.357, heading: 1.039 }] },
    ]);
  });

  test('times may arrive as text', () => {
    const [trail] = trails([frame({ time: ['2026-09-21T10:23:07.389Z', '1789986188447'], x: [0, 1], y: [0, 0] })]);
    expect(trail.points.map((p) => p.time)).toEqual([1789986187389, 1789986188447]);
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
    expect(trail.points.map((p) => new Date(p.time).toISOString())).toEqual([
      '2026-09-21T10:23:07.000Z',
      '2026-09-21T10:23:08.500Z',
      '2026-09-21T10:23:09.000Z',
    ]);
  });

  test('rows without a usable position are skipped, missing optional values are left out', () => {
    const [trail] = trails([
      frame({ time: [1000, 2000, 3000, null], x: [1, null, 3, 4], y: [1, 2, 'n/a', 4], theta: [null, 0, 0, 0] }),
    ]);
    expect(trail.points).toEqual([{ time: 1000, x: 1, y: 1 }]);
  });

  test.each([
    ['no data at all', []],
    ['a query that returned no rows', [frame({ time: [], x: [], y: [] })]],
    ['a query that returned nothing usable', [frame({ time: [1000], x: [null], y: [null] })]],
    ['an empty frame without fields', [createDataFrame({ fields: [] })]],
  ])('an empty range (%s) has no Trails', (_, frames) => {
    expect(trails(frames)).toEqual([]);
  });

  test('frames without the Trail columns, such as another query, are ignored', () => {
    const result = trails([frame({ zone: [1], progress: [50] }), frame({ time: [1000], x: [1], y: [1] })]);
    expect(result).toHaveLength(1);
  });

  test('an empty Trail query beside another query with rows is an empty range, not a problem', () => {
    const zoneProgress = frame({ zone: [1, 2], progress: [50, 10], time: [1, 2] });
    expect(readTrails([frame({ time: [], x: [], y: [] }), zoneProgress])).toEqual({ trails: [] });
  });

  test('the problem names what the closest frame lacks, not whichever came first', () => {
    const zoneProgress = frame({ zone: [1], progress: [50], time: [1] });
    expect(readTrails([zoneProgress, frame({ time: [1000], x: [1], northing: [1] })])).toEqual({
      problem: expect.stringMatching(/^No "y" column for/),
    });
  });

  test('missing required columns are named, with where to fix them', () => {
    expect(readTrails([frame({ time: [1000], postureX: [1], postureY: [1] })], {})).toEqual({
      problem: expect.stringMatching(/"x" and "y".*Trail columns/),
    });
    expect(readTrails([frame({ time: [1000], x: [1] })], { y: 'northing' })).toEqual({
      problem: expect.stringMatching(/"northing"/),
    });
  });
});
