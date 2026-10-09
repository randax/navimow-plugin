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
/** A number field, of a series when it has labels. */
const numbers = (name: string, values: unknown[], labels?: Record<string, string>) => ({
  name,
  type: FieldType.number,
  values,
  labels,
});

/** The names of the columns Grafana's Time series format keeps as fields: all but the time and the labels. */
const numbered = (columns: Columns, labelled: string[]): string[] =>
  Object.keys(columns).filter((name) => name !== 'time' && !labelled.includes(name));

/**
 * The rows of each set of labels, the sets in the order Grafana puts them. A row without a value for
 * a label has it as an empty text.
 */
const labelSets = (columns: Columns, labelled: string[]) => {
  const sets = new Map<string, { labels: Record<string, string>; rows: number[] }>();
  columns.time.forEach((_, row) => {
    const labels = Object.fromEntries(labelled.map((name) => [name, String(columns[name][row] ?? '')]));
    const key = JSON.stringify(labels);
    sets.set(key, { labels, rows: [...(sets.get(key)?.rows ?? []), row] });
  });
  return [...sets].sort(([a], [b]) => (a < b ? -1 : 1)).map(([, set]) => set);
};

/**
 * The rows as Grafana's Time series format hands them to a panel: one frame with a row for each
 * time, named Time, the named columns as labels, and each set of labels with number fields of its
 * own, empty wherever a row is another set's.
 */
const wide = (columns: Columns, labelled: string[]): DataFrame => {
  const times = [...new Set(columns.time)].sort((a, b) => Number(a) - Number(b));
  return createDataFrame({
    refId: 'A',
    fields: [
      { name: 'Time', type: FieldType.time, values: times },
      ...numbered(columns, labelled).flatMap((name) =>
        labelSets(columns, labelled).map(({ labels, rows }) =>
          numbers(
            name,
            times.map((time) => columns[name][rows.find((row) => columns.time[row] === time) ?? -1] ?? null),
            labels
          )
        )
      ),
    ],
  });
};

/** The rows as a frame for each set of labels, which is how other versions and data sources hand a time series over. */
const perSeries = (columns: Columns, labelled: string[]): DataFrame[] =>
  labelSets(columns, labelled).map(({ labels, rows }) =>
    createDataFrame({
      refId: 'A',
      fields: [
        { name: 'Time', type: FieldType.time, values: rows.map((row) => columns.time[row]) },
        ...numbered(columns, labelled).map((name) =>
          numbers(
            name,
            rows.map((row) => columns[name][row]),
            labels
          )
        ),
      ],
    })
  );

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

  // Some data sources and versions of Grafana split a time series into a frame per value of a text column.
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

  test('a time field by another name than Time is not taken for the time', () => {
    const table = createDataFrame({
      fields: [
        { name: 'received_time', type: FieldType.time, values: at(0) },
        { name: 'x', type: FieldType.number, values: [1] },
        { name: 'y', type: FieldType.number, values: [2] },
      ],
    });
    expect(readTrails([table], { time: 'device_time' })).toEqual({
      problem: expect.stringMatching(/^No "device_time" column for/),
    });
  });

  test('positions that are there, but in no series with the other columns, are a problem and not an empty range', () => {
    const series = wide({ time: at(0, SEC), x: [1, 2], y: [0, 0], job_id: ['a', 'b'] }, ['job_id']);
    expect(readTrails([series], { x: 'Time' })).toEqual({
      problem:
        'No series has all of the "time", "Time" and "y" columns: they are fields with different labels. ' +
        'Check which columns are set under Trail columns in the panel options.',
    });
  });

  test('a field is read before a label of its name', () => {
    const labels = { job_id: 'job-1', zone: 'from the label' };
    const series = createDataFrame({
      fields: [
        { name: 'time', type: FieldType.time, values: at(0) },
        numbers('x', [1], labels),
        numbers('y', [2], labels),
        numbers('zone', [8], labels),
      ],
    });
    expect(trails([series])[0].segments[0][0].zone).toBe('8');
  });

  test('fields are of one series by their labels, in whatever order the labels are written', () => {
    const series = createDataFrame({
      fields: [
        { name: 'Time', type: FieldType.time, values: at(0, SEC) },
        numbers('x', [1, null], { job_id: 'a', device_id: 'm1' }),
        numbers('x', [null, 5], { job_id: 'b', device_id: 'm1' }),
        numbers('y', [2, null], { device_id: 'm1', job_id: 'a' }),
        numbers('y', [null, 6], { device_id: 'm1', job_id: 'b' }),
      ],
    });
    expect(trails([series]).map((t) => [t.job, t.segments[0][0].x, t.segments[0][0].y])).toEqual([
      ['a', 1, 2],
      ['b', 5, 6],
    ]);
    // Names and values that read alike once written out in a row.
    const alike = createDataFrame({
      fields: [
        { name: 'Time', type: FieldType.time, values: at(0) },
        numbers('x', [1], { a: 'b,c', 'a,b': 'c' }),
        numbers('y', [2], { 'a,b': 'c', a: 'b,c' }),
        numbers('y', [9], { a: 'other' }),
      ],
    });
    expect(xs(trails([alike]))).toEqual([[undefined, [[1]]]]);
    // And two sets of labels that are not the same, though they read alike.
    const unlike = createDataFrame({
      fields: [
        { name: 'Time', type: FieldType.time, values: at(0) },
        numbers('x', [1], { a: 'b', c: 'd' }),
        numbers('x', [7], { z: '1' }),
        numbers('y', [2], { a: 'b,c=d' }),
        numbers('y', [8], { z: '1' }),
      ],
    });
    expect(xs(trails([unlike]))).toEqual([[undefined, [[7]]]]);
  });

  test("a series without a column is not given another series' field for it", () => {
    // Three mowers at one moment, so that every series has the row.
    const [a, b, c] = ['a', 'b', 'c'].map((device_id) => ({ device_id }));
    const headings = (...thetas: Array<ReturnType<typeof numbers>>) =>
      trails([
        createDataFrame({
          fields: [
            { name: 'Time', type: FieldType.time, values: at(0) },
            ...[a, b, c].flatMap((labels) => [numbers('x', [1], labels), numbers('y', [2], labels)]),
            ...thetas,
          ],
        }),
      ]).map((t) => t.segments[0][0].heading);
    // Whether the column is another series' alone, or two others'.
    expect(headings(numbers('theta', [0.5], a))).toEqual([0.5, undefined, undefined]);
    expect(headings(numbers('theta', [0.5], a), numbers('theta', [1.5], b))).toEqual([0.5, 1.5, undefined]);
  });

  test('a series with an x and no y of its own has no positions, and borrows none', () => {
    const series = createDataFrame({
      fields: [
        { name: 'Time', type: FieldType.time, values: at(0) },
        numbers('x', [1], { job_id: 'a' }),
        numbers('x', [2], { job_id: 'b' }),
        numbers('y', [3], { job_id: 'a' }),
      ],
    });
    expect(trails([series])).toEqual([{ job: 'a', segments: [[{ time: T, x: 1, y: 3 }]] }]);
  });

  test('of two columns of one name in a Table, as a join gives, the first is read, and each row once', () => {
    const joined = createDataFrame({
      fields: [
        { name: 'time', type: FieldType.time, values: at(0, SEC) },
        { name: 'time', type: FieldType.time, values: at(MIN, 2 * MIN) },
        numbers('x', [1, 2]),
        numbers('y', [0, 0]),
        numbers('x', [8, 9]),
        { name: 'job_id', type: FieldType.string, values: ['a', 'a'] },
        { name: 'job_id', type: FieldType.string, values: [null, null] },
      ],
    });
    expect(trails([joined])).toEqual([
      {
        job: 'a',
        segments: [
          [
            { time: T, x: 1, y: 0 },
            { time: T + SEC, x: 2, y: 0 },
          ],
        ],
      },
    ]);
  });

  test('a column by the name of the time column is read before one named Time', () => {
    const both = createDataFrame({
      fields: [
        { name: 'Time', type: FieldType.time, values: at(MIN) },
        { name: 'time', type: FieldType.time, values: at(0) },
        numbers('x', [1]),
        numbers('y', [2]),
      ],
    });
    expect(trails([both])[0].segments[0][0].time).toBe(T);
  });

  // What joining metrics by time and renaming them gives: each field with the name of its own
  // metric among its labels.
  test('metrics joined by time are a series for each of their other labels', () => {
    const metric = (name: string, values: unknown[], job_id: string) =>
      numbers(name, values, { __name__: `mower_${name}`, job_id });
    const joined = createDataFrame({
      fields: [
        { name: 'Time', type: FieldType.time, values: at(0, SEC, 2 * SEC, 3 * SEC) },
        metric('x', [1, 2, null, null], 'a'),
        metric('x', [null, null, 5, 6], 'b'),
        metric('y', [3, 4, null, null], 'a'),
        metric('y', [null, null, 7, 8], 'b'),
        metric('theta', [0.1, 0.2, null, null], 'a'),
        metric('theta', [null, null, 0.5, 0.6], 'b'),
      ],
    });
    expect(
      trails([joined]).map((t) => [t.job, t.segments.map((points) => points.map((p) => [p.x, p.y, p.heading]))])
    ).toEqual([
      [
        'a',
        [
          [
            [1, 3, 0.1],
            [2, 4, 0.2],
          ],
        ],
      ],
      [
        'b',
        [
          [
            [5, 7, 0.5],
            [6, 8, 0.6],
          ],
        ],
      ],
    ]);
  });

  test('a frame with one x is one series with the only field of each other name, whatever labels they carry', () => {
    const joined = createDataFrame({
      fields: [
        { name: 'Time', type: FieldType.time, values: at(0, SEC) },
        numbers('x', [1, 2], { sensor: 'east', job_id: 'a' }),
        numbers('y', [3, 4], { sensor: 'north', job_id: 'a' }),
        numbers('theta', [0.1, 0.2], { sensor: 'compass' }),
      ],
    });
    expect(trails([joined])).toEqual([
      {
        job: 'a',
        segments: [
          [
            { time: T, x: 1, y: 3, heading: 0.1 },
            { time: T + SEC, x: 2, y: 4, heading: 0.2 },
          ],
        ],
      },
    ]);
  });

  test.each([
    ['x', { job_id: 'job-1' }, undefined],
    ['y', undefined, { job_id: 'job-1' }],
  ])('a label on the %s field alone is read', (_, onX, onY) => {
    const series = createDataFrame({
      fields: [{ name: 'time', type: FieldType.time, values: at(0) }, numbers('x', [1], onX), numbers('y', [2], onY)],
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
      // The row is of the Job whose labels come last, so that the line to break is not the first series' own.
      const gapped = {
        time: at(0, SEC, 2 * SEC, 3 * SEC),
        x: [0, null, 2, 3],
        y: [0, null, 0, 0],
        job_id: ['b', 'b', 'b', 'a'],
      };
      const expected = [
        ['b', [[0], [2]]],
        ['a', [[3]]],
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

    // A number is no text, so a numbered mower is a field of each set of labels, with a value in the
    // very row that has no position.
    test('a row without a position breaks the line of the mower a number column names', () => {
      const numbered = {
        time: at(0, SEC, 2 * SEC, 3 * SEC),
        x: [0, null, 2, 3],
        y: [0, null, 0, 0],
        device_id: [7, 7, 7, 7],
        job_id: ['b', 'b', 'b', 'a'],
      };
      const expected = [
        ['b', [[0], [2]]],
        ['a', [[3]]],
      ];
      expect(xs(trails([frame(numbered)]))).toEqual(expected);
      expect(xs(trails([wide(numbered, ['job_id'])]))).toEqual(expected);
    });

    // What the Time series format cannot carry, as the README says: whose such a row is.
    test("with several mowers, a row empty for every set of labels breaks every mower's line", () => {
      const gapped = {
        time: at(0, SEC, 2 * SEC, 3 * SEC, 4 * SEC),
        x: [0, 10, null, 2, 11],
        y: [0, 0, null, 0, 0],
        device_id: ['m1', 'm2', 'm1', 'm1', 'm2'],
      };
      expect(xs(trails([frame(gapped)]))).toEqual([
        [undefined, [[0], [2]]],
        [undefined, [[10, 11]]],
      ]);
      expect(xs(trails([wide(gapped, ['device_id'])]))).toEqual([
        [undefined, [[0], [2]]],
        [undefined, [[10], [11]]],
      ]);
    });

    test('two mowers without a Job are a Trail each, neither breaking the other', () => {
      const mowers = {
        time: at(0, SEC, 2 * SEC, 3 * SEC),
        x: [0, 10, 1, 11],
        y: [0, 0, 0, 0],
        device_id: ['m1', 'm2', 'm1', 'm2'],
      };
      const expected = [
        [undefined, [[0, 1]]],
        [undefined, [[10, 11]]],
      ];
      expect(xs(trails([frame(mowers)]))).toEqual(expected);
      expect(xs(trails([wide(mowers, ['device_id'])]))).toEqual(expected);
      expect(xs(trails(perSeries(mowers, ['device_id'])))).toEqual(expected);
    });

    test("a row with half a position is its own mower's, and breaks no line but that mower's", () => {
      const halved = {
        time: at(0, SEC, 2 * SEC, 3 * SEC, 4 * SEC, 5 * SEC, 6 * SEC, 7 * SEC),
        x: [0, 10, null, 11, 2, 5, 12, 3],
        y: [0, 0, 5, 0, 0, null, 0, 0],
        job_id: ['a', 'b', 'a', 'b', 'a', 'a', 'b', 'a'],
        device_id: ['m1', 'm2', 'm1', 'm2', 'm1', 'm1', 'm2', 'm1'],
      };
      const expected = [
        ['a', [[0], [2], [3]]],
        ['b', [[10, 11, 12]]],
      ];
      expect(xs(trails([frame(halved)]))).toEqual(expected);
      expect(xs(trails([wide(halved, ['job_id', 'device_id'])]))).toEqual(expected);
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

  test('frames that do not say which query they are of are each a Trail of their own', () => {
    const result = trails([frame({ time: at(MIN), x: [3], y: [0] }), frame({ time: at(0), x: [1], y: [0] })]);
    expect(xs(result)).toEqual([
      [undefined, [[1]]],
      [undefined, [[3]]],
    ]);
  });

  test.each([
    ['A', 'b:c', 'A:b', 'c'],
    ['A', 'bc', 'Ab', 'c'],
  ])('query %s with mower %s is not taken for query %s with mower %s', (query, mower, other, its) => {
    const result = trails([
      { ...frame({ time: at(0), x: [1], y: [0], device_id: [mower] }), refId: query },
      { ...frame({ time: at(SEC), x: [9], y: [0], device_id: [its] }), refId: other },
    ]);
    expect(xs(result)).toEqual([
      [undefined, [[1]]],
      [undefined, [[9]]],
    ]);
  });

  test('a query named as a number is not taken for the frame of that number', () => {
    const result = trails([
      { ...frame({ time: at(0), x: [1], y: [0] }), refId: '1' },
      frame({ time: at(SEC), x: [9], y: [0] }),
    ]);
    expect(xs(result)).toEqual([
      [undefined, [[1]]],
      [undefined, [[9]]],
    ]);
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

  test('text positions with a decimal comma, as a Norwegian spreadsheet writes them, are read as decimals', () => {
    const csv = frame({ time: at(0, SEC), x: ['-0,31', '1,0'], y: ['-0,357', '2'], theta: ['1,039', '-0,5'] });
    expect(trails([csv])).toEqual([
      {
        segments: [
          [
            { time: T, x: -0.31, y: -0.357, heading: 1.039 },
            { time: T + SEC, x: 1, y: 2, heading: -0.5 },
          ],
        ],
      },
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
    ['x', { time: at(0, SEC), x: ['1.234,5', '-2.000,25'], y: [0, 0] }, '1.234,5'],
    ['y', { time: at(0, SEC), x: [0, 0], y: ['1,234,5', '1 234,5'] }, '1,234,5'],
    ['time', { time: ['21.09.2026 10:23:07'], x: [0], y: [0] }, '21.09.2026 10:23:07'],
    ['time', { time: ['2026-09-21T10:00:00+02'], x: [0], y: [0] }, '2026-09-21T10:00:00+02'],
  ])('a "%s" column with nothing readable says so, with an example', (column, columns, example) => {
    expect(readTrails([frame(columns)])).toEqual({
      problem: expect.stringContaining(`"${column}" column has no value the Trail can read, such as "${example}"`),
    });
  });

  test('unreadable positions are told how to write them', () => {
    expect(readTrails([frame({ time: at(0), x: ['1.234,5'], y: [0] })])).toEqual({
      problem: expect.stringContaining(
        'Positions must be numbers of metres, such as -0.31 or -0,31, without grouped digits.'
      ),
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
