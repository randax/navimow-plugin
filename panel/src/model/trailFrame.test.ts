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
/** The label of each named column in a row; Grafana labels a row without a value with an empty text. */
const labelsOf = (columns: Columns, labelled: string[], row: number): Record<string, string> =>
  Object.fromEntries(labelled.map((name) => [name, String(columns[name][row] ?? '')]));

/**
 * The rows as Grafana's Time series format hands them to a panel: one frame, the time named Time,
 * the named columns as labels, and each set of labels with number fields of its own, empty wherever
 * a row is another set's.
 */
const wide = (columns: Columns, labelled: string[]): DataFrame => {
  const rows = columns.time.map((_, row) => labelsOf(columns, labelled, row));
  const sets = new Map(rows.map((labels) => [JSON.stringify(labels), labels]));
  const numbers = Object.keys(columns).filter((name) => name !== 'time' && !labelled.includes(name));
  return createDataFrame({
    refId: 'A',
    fields: [
      { name: 'Time', type: FieldType.time, values: columns.time },
      ...numbers.flatMap((name) =>
        [...sets].map(([set, labels]) => ({
          name,
          type: FieldType.number,
          labels,
          values: columns[name].map((value, row) => (JSON.stringify(rows[row]) === set ? value : null)),
        }))
      ),
    ],
  });
};

/** The rows as a frame for each set of labels, which is how other versions and data sources hand a time series over. */
const perSeries = (columns: Columns, labelled: string[]): DataFrame[] => {
  const all = wide(columns, labelled);
  const sets = [...new Set(all.fields.slice(1).map((f) => JSON.stringify(f.labels)))];
  return sets.map((set) => {
    const fields = all.fields.filter((f, i) => i === 0 || JSON.stringify(f.labels) === set);
    const own = columns.time.flatMap((_, row) => (fields.slice(1).some((f) => f.values[row] !== null) ? [row] : []));
    return createDataFrame({
      refId: all.refId,
      fields: fields.map((f) => ({ ...f, values: own.map((row) => f.values[row]) })),
    });
  });
};

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

  // Grafana's Time series format turns the text columns of a SQL query into labels on its number fields.
  test('a column that is not a field is read from the label of that name on the position fields', () => {
    const labels = { job_id: 'job-1', zone: 'front', status: 'isRunning', device_id: 'mower-1' };
    const series = createDataFrame({
      fields: [
        { name: 'time', type: FieldType.time, values: at(0, SEC) },
        { name: 'x', type: FieldType.number, values: [1, 2], labels },
        { name: 'y', type: FieldType.number, values: [3, 4], labels },
      ],
    });
    expect(trails([series])).toEqual([
      {
        job: 'job-1',
        segments: [
          [
            { time: T, x: 1, y: 3, zone: 'front', status: 'isRunning', mower: 'mower-1' },
            { time: T + SEC, x: 2, y: 4, zone: 'front', status: 'isRunning', mower: 'mower-1' },
          ],
        ],
      },
    ]);
  });

  test('two time fields, neither by the name of the time column, are not guessed between', () => {
    const table = createDataFrame({
      fields: [
        { name: 'received_time', type: FieldType.time, values: at(SEC) },
        { name: 'device_time', type: FieldType.time, values: at(0) },
        { name: 'x', type: FieldType.number, values: [1] },
        { name: 'y', type: FieldType.number, values: [2] },
      ],
    });
    expect(readTrails([table])).toEqual({ problem: expect.stringMatching(/^No "time" column for/) });
  });

  test('a label on either position field is read', () => {
    const series = createDataFrame({
      fields: [
        { name: 'time', type: FieldType.time, values: at(0) },
        { name: 'x', type: FieldType.number, values: [1] },
        { name: 'y', type: FieldType.number, values: [2], labels: { job_id: 'job-1' } },
      ],
    });
    expect(trails([series])).toEqual([{ job: 'job-1', segments: [[{ time: T, x: 1, y: 2 }]] }]);
  });

  test('without a column of the name, the time is the time field: the Time series format names it Time', () => {
    const series = createDataFrame({
      fields: [
        { name: 'Time', type: FieldType.time, values: at(0, SEC) },
        { name: 'x', type: FieldType.number, values: [1, 2] },
        { name: 'y', type: FieldType.number, values: [3, 4] },
      ],
    });
    expect(xs(trails([series]))).toEqual([[undefined, [[1, 2]]]]);
  });

  describe('the same rows in the Table and the Time series format', () => {
    // Job a through two Zones with twenty minutes of silence in it, Job b, and a position after it
    // that belongs to no Job.
    const table = {
      time: at(0, SEC, 2 * SEC, 22 * MIN, 23 * MIN, 24 * MIN),
      x: [1, 2, 3, 4, 5, 0],
      y: [0, 1, 2, 3, 4, 0],
      theta: [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
      job_id: ['a', 'a', 'a', 'a', 'b', null],
      zone: ['front', 'front', 'back', 'back', 'front', null],
      status: ['isRunning', 'isRunning', 'isRunning', 'isRunning', 'isRunning', 'isDocked'],
      device_id: ['m1', 'm1', 'm1', 'm1', 'm1', 'm1'],
    };
    const text = ['job_id', 'zone', 'status', 'device_id'];

    test('as a Table, they are a Trail for each Job and one for outside any', () => {
      const result = trails([frame(table)]);
      expect(xs(result)).toEqual([
        ['a', [[1, 2, 3], [4]]],
        ['b', [[5]]],
        [undefined, [[0]]],
      ]);
      expect(result[0].segments[0][2]).toEqual({
        time: T + 2 * SEC,
        x: 3,
        y: 2,
        heading: 0.3,
        zone: 'back',
        status: 'isRunning',
        mower: 'm1',
      });
    });

    test('as one frame with a field for each set of labels, they are the same Trails', () => {
      expect(trails([wide(table, text)])).toEqual(trails([frame(table)]));
    });

    test('as a frame for each set of labels, they are the same Trails', () => {
      expect(trails(perSeries(table, text))).toEqual(trails([frame(table)]));
    });

    test('a row without a position, empty for every set of labels, still breaks the line', () => {
      const gapped = {
        time: at(0, SEC, 2 * SEC, 3 * SEC),
        x: [0, null, 2, 3],
        y: [0, null, 0, 0],
        job_id: ['a', 'a', 'a', 'b'],
      };
      const expected = [
        ['a', [[0], [2]]],
        ['b', [[3]]],
      ];
      expect(xs(trails([frame(gapped)]))).toEqual(expected);
      expect(xs(trails([wide(gapped, ['job_id'])]))).toEqual(expected);
    });

    // A numbered Zone is no text, so it stays a field: one for each set of labels, like x and y.
    test('a number column is read from the field of its own set of labels', () => {
      const numbered = { ...table, zone: [8, 8, 9, 9, 8, null] };
      const result = trails([wide(numbered, ['job_id', 'status', 'device_id'])]);
      expect(result).toEqual(trails([frame(numbered)]));
      expect(result.map((t) => t.segments.flat().map((p) => p.zone))).toEqual([
        ['8', '8', '9', '9'],
        ['8'],
        [undefined],
      ]);
    });

    test('without a Job, a query split into frames by its other text columns is still one Trail', () => {
      const { job_id, device_id, ...jobless } = table;
      const split = perSeries(jobless, ['zone', 'status']);
      expect(split).toHaveLength(3);
      expect(xs(trails(split))).toEqual([
        [
          undefined,
          [
            [1, 2, 3],
            [4, 5, 0],
          ],
        ],
      ]);
    });
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

  test('without a Job column, each query is its own Trail', () => {
    const result = trails([
      { ...frame({ time: at(MIN), x: [3], y: [0] }), refId: 'A' },
      { ...frame({ time: at(0), x: [1], y: [0] }), refId: 'B' },
    ]);
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

  test('a position more than 10 km from the dock is left out, and the line breaks where it was', () => {
    const result = trails([frame({ time: at(0, SEC, 2 * SEC, 3 * SEC), x: [1, 2, 9000, 4], y: [0, 0, 9000, 0] })]);
    expect(xs(result)).toEqual([[undefined, [[1, 2], [4]]]]);
  });

  test('a position 10 km from the dock is still a position', () => {
    const result = trails([frame({ time: at(0, SEC), x: [6000, -10_000], y: [8000, 0] })]);
    expect(xs(result)).toEqual([[undefined, [[6000, -10_000]]]]);
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

  test('x set to the time column, so epoch milliseconds are read as metres, names both columns and the distance', () => {
    expect(readTrails([frame({ time: at(0, SEC), y: [1, 2] })], { x: 'time' })).toEqual({
      problem:
        'The "time" and "y" columns put every position more than 10 km from the dock, the first 1,789,986,187 km away. ' +
        'Positions must be metres from the dock: check which columns are set under Trail columns in the panel options.',
    });
  });

  test('a position only just out of range is not said to be 10 km away, which would be in range', () => {
    expect(readTrails([frame({ time: at(0), x: [10_001], y: [0] })])).toEqual({
      problem: expect.stringContaining('more than 10 km from the dock, the first 10.1 km away.'),
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
