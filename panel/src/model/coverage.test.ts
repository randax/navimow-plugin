import {
  coverageGrid,
  coverageSettings,
  coverageState,
  type CoverageOptions,
  type CoverageState,
} from './coverage';
import type { Trail, TrailPoint } from './trailFrame';

const START = Date.UTC(2026, 8, 30, 10);
const SEC = 1000;

const at = (seconds: number, x: number, y: number): TrailPoint => ({ time: START + seconds * SEC, x, y });
/** A Trail drawn in one unbroken run. */
const run = (points: TrailPoint[], job?: string): Trail => ({ job, segments: [points] });
/** Each cell as its corner nearest the dock's south-west, with what the test is about. */
const corners = (cells: Array<{ x: number; y: number }>) => cells.map(({ x, y }) => [x, y]).sort();

describe('coverageGrid', () => {
  // Half-metre cells, the mower driving east along the middle of the row that starts at the dock.
  const east = [at(0, 0.25, 0.25), at(2, 1.25, 0.25), at(4, 2.25, 0.25)];
  const row = [
    [0, 0],
    [0.5, 0],
    [1, 0],
    [1.5, 0],
    [2, 0],
  ];

  test('a pass cuts every cell under it, the ones between positions included, once each', () => {
    const { cells } = coverageGrid([run(east)], { cellSize: 0.5, cuttingWidth: 0.43 });
    expect(corners(cells)).toEqual(row);
    expect(cells.map((c) => c.visits)).toEqual([1, 1, 1, 1, 1]);
  });

  test('a deck wide enough to reach the middles of the rows beside it cuts those too', () => {
    const { cells } = coverageGrid([run(east)], { cellSize: 0.5, cuttingWidth: 1.2 });
    // The same five columns in the row below, the row itself and the row above, and one cell past
    // each end of the pass: their middles are 0.5 m from where it started and stopped. The cells
    // diagonally past the ends are 0.71 m away, out of a 0.6 m reach.
    const beside = [-0.5, 0, 0.5].flatMap((y) => row.map(([x]) => [x, y]));
    expect(corners(cells)).toEqual([...beside, [-0.5, 0], [2.5, 0]].sort());
  });

  test('cells wider than the deck are still cut by a pass through them', () => {
    const { cells } = coverageGrid([run(east)], { cellSize: 2, cuttingWidth: 0.43 });
    expect(corners(cells)).toEqual([
      [0, 0],
      [2, 0],
    ]);
  });

  test('coming back over the same ground later is a second visit; lingering on it is not', () => {
    const lingering = [at(14, 2.25, 0.25), at(24, 2.25, 0.25), at(30, 2.25, 0.25)];
    const back = [at(32, 1.25, 0.25), at(34, 0.25, 0.25)];
    const { cells } = coverageGrid([run([...east, ...lingering, ...back])], { cellSize: 0.5, cuttingWidth: 0.43 });
    const visits = Object.fromEntries(cells.map((c) => [c.x, c.visits]));
    // The far cell was never left; the others were, for half a minute.
    expect(visits).toEqual({ 0: 2, 0.5: 2, 1: 2, 1.5: 2, 2: 1 });
  });

  test('a docked mower cuts nothing, however long it reports from the dock', () => {
    // A heartbeat every five minutes, drifting by a centimetre or two as a real dock's does.
    const docked = [at(0, -0.31, -0.357), at(300, -0.322, -0.39), at(600, -0.286, -0.363), at(900, -0.31, -0.358)];
    expect(coverageGrid([run(docked)], { cellSize: 0.5, cuttingWidth: 0.43 }).cells).toEqual([]);
  });

  test('positions either side of a silence are not joined: where the mower went in between is unknown', () => {
    const before = [at(0, 0.25, 0.25), at(2, 0.75, 0.25)];
    const after = [at(120, 10.25, 0.25), at(122, 10.75, 0.25)];
    const { cells } = coverageGrid([run([...before, ...after])], { cellSize: 0.5, cuttingWidth: 0.43 });
    expect(corners(cells)).toEqual([
      [0, 0],
      [0.5, 0],
      [10, 0],
      [10.5, 0],
    ]);
  });

  test('each cell remembers when it was last cut, whichever order the Trails come in', () => {
    const monday = run(east, 'monday');
    const wednesday = run(
      east.slice(0, 2).map((p) => ({ ...p, time: p.time + 2 * 86_400 * SEC })),
      'wednesday'
    );
    const { cells } = coverageGrid([wednesday, monday], { cellSize: 0.5, cuttingWidth: 0.43 });
    const cut = Object.fromEntries(cells.map((c) => [c.x, [c.visits, c.last - START]]));
    // Half a metre a second, so a pass is over the middle of each cell a second after the last.
    // Wednesday's stopped half way along the row.
    const wednesdayAt = (seconds: number) => (2 * 86_400 + seconds) * SEC;
    expect(cut).toEqual({
      0: [2, wednesdayAt(0)],
      0.5: [2, wednesdayAt(1)],
      1: [2, wednesdayAt(2)],
      1.5: [1, 3 * SEC],
      2: [1, 4 * SEC],
    });
  });
});

describe('coverageState', () => {
  /** The panel's state after the options change, in turn, and the owner switches on the panel in between. */
  const after = (...steps: Array<{ options?: CoverageOptions } | { switched: Partial<CoverageState> }>) =>
    steps.reduce<CoverageState | undefined>(
      (state, step) =>
        'switched' in step ? state && { ...state, ...step.switched } : coverageState(step.options, state),
      undefined
    );

  test('the panel opens as a flat Grid unless its options start it otherwise', () => {
    expect(after({})).toMatchObject({ style: 'grid', raised: false });
    expect(after({ options: { style: 'heatmap', raised: true } })).toMatchObject({ style: 'heatmap', raised: true });
  });

  test.each([null, { style: 'cells' }, { style: 7, raised: 'yes' }])(
    'options saved as %p open it as a flat Grid',
    (saved) => {
      expect(coverageState(saved as unknown as CoverageOptions)).toMatchObject({ style: 'grid', raised: false });
    }
  );

  test('a switch on the panel holds while other options change', () => {
    const switched = after(
      { options: { style: 'grid' } },
      { switched: { style: 'line', raised: true } },
      { options: { style: 'grid', cellSize: 1 } }
    );
    expect(switched).toMatchObject({ style: 'line', raised: true });
  });

  test('a new start in the options starts the panel over from them', () => {
    const restyled = after(
      { options: { style: 'grid' } },
      { switched: { style: 'line' } },
      { options: { style: 'heatmap' } }
    );
    expect(restyled).toMatchObject({ style: 'heatmap' });
    const lowered = after({ options: {} }, { switched: { raised: true } }, { options: { raised: true } });
    expect(lowered).toMatchObject({ raised: true });
    expect(after({ options: { raised: true } }, { switched: { raised: false } }, { options: {} })).toMatchObject({
      raised: false,
    });
  });
});

describe('coverageSettings', () => {
  const shown = coverageState(undefined);

  test('by default cells are half a metre, the deck 0.43 m, and colour tells visit count', () => {
    expect(coverageSettings(undefined, shown)).toEqual({
      style: 'grid',
      raised: false,
      encoding: 'visits',
      cellSize: 0.5,
      cuttingWidth: 0.43,
    });
  });

  test('the options set the encoding and sizes; the panel sets the style', () => {
    const options: CoverageOptions = { style: 'grid', encoding: 'age', cellSize: 1, cuttingWidth: 0.24 };
    expect(coverageSettings(options, { ...shown, style: 'heatmap', raised: true })).toEqual({
      style: 'heatmap',
      raised: true,
      encoding: 'age',
      cellSize: 1,
      cuttingWidth: 0.24,
    });
  });

  test('sizes too small to draw, too large to mean anything or not numbers at all are brought into range', () => {
    const sizes = (cellSize: unknown, cuttingWidth: unknown) => {
      const settings = coverageSettings({ cellSize, cuttingWidth } as CoverageOptions, shown);
      return [settings.cellSize, settings.cuttingWidth];
    };
    expect(sizes(0.01, 0)).toEqual([0.25, 0.1]);
    expect(sizes(100, 9)).toEqual([5, 2]);
    expect(sizes('wide', null)).toEqual([0.5, 0.43]);
    expect(sizes(NaN, -1)).toEqual([0.5, 0.1]);
  });
});
