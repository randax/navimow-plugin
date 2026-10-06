/**
 * Coverage as the mower left it, before any map: how the owner has asked to see it, and the grid of
 * cells the Trail has cut. What the map draws from these is coverageScene.ts.
 */
import type { Trail, TrailPoint } from './trailFrame';

/** The three Coverage styles. */
export type CoverageStyle = 'grid' | 'heatmap' | 'line';

/** What colour and height tell: how often each part was cut, or how long ago. */
export type CoverageEncoding = 'visits' | 'age';

/** The styles and encodings, as the panel and the editor name them. The first of each is the default. */
export const COVERAGE_STYLES: Array<{ value: CoverageStyle; label: string }> = [
  { value: 'grid', label: 'Grid' },
  { value: 'heatmap', label: 'Heatmap' },
  { value: 'line', label: 'Buffered line' },
];
export const COVERAGE_ENCODINGS: Array<{ value: CoverageEncoding; label: string }> = [
  { value: 'visits', label: 'Visit count' },
  { value: 'age', label: 'Time since mowed' },
];

/** A size in metres the owner sets: what it is until they do, and the range that draws sensibly. */
export interface Size {
  default: number;
  min: number;
  max: number;
}
// Half a metre reads cleanly from above, on a real Job and a real lawn. A quarter is finer and four
// times the cells; much below that the grid is heavier than the Trail it was made from.
export const CELL_SIZE: Size = { default: 0.5, min: 0.25, max: 5 };
// The X4 series cuts 43 cm; the smallest Navimow, 18 cm.
export const CUTTING_WIDTH: Size = { default: 0.43, min: 0.1, max: 2 };

/** How Coverage is to be drawn, with every choice made. */
export interface CoverageSettings extends GridOptions {
  style: CoverageStyle;
  /** Drawn standing up from the ground, to be looked at from the side, rather than flat on it. */
  raised: boolean;
  encoding: CoverageEncoding;
}

/** Coverage as the panel options save it. */
export interface CoverageOptions {
  /** The style the panel opens in. */
  style?: CoverageStyle;
  /** Whether the panel opens with Coverage raised. */
  raised?: boolean;
  encoding?: CoverageEncoding;
  cellSize?: number;
  cuttingWidth?: number;
}

/** How Coverage is shown on the panel, and the options that was started from. */
export interface CoverageState {
  start: { style: CoverageStyle; raised: boolean };
  style: CoverageStyle;
  raised: boolean;
}

// Options are typed in, or edited as panel JSON, so anything can arrive.
const oneOf = <T extends string>(known: Array<{ value: T }>, saved: unknown): T =>
  known.find(({ value }) => value === saved)?.value ?? known[0].value;
const sized = ({ default: unset, min, max }: Size, saved: unknown): number =>
  typeof saved === 'number' && !Number.isNaN(saved) ? Math.min(Math.max(saved, min), max) : unset;

/**
 * How to show Coverage, given how it was shown before. Like the view, the owner's switch on the
 * panel holds for as long as the options start the panel the same way, and a new start in the
 * options starts the panel over from them.
 */
export function coverageState(options: CoverageOptions | null | undefined, previous?: CoverageState): CoverageState {
  const start = { style: oneOf(COVERAGE_STYLES, options?.style), raised: options?.raised === true };
  return previous && previous.start.style === start.style && previous.start.raised === start.raised
    ? previous
    : { start, ...start };
}

/** Every choice about drawing Coverage: the panel's switches, and the rest from the options. */
export const coverageSettings = (
  options: CoverageOptions | null | undefined,
  { style, raised }: CoverageState
): CoverageSettings => ({
  style,
  raised,
  encoding: oneOf(COVERAGE_ENCODINGS, options?.encoding),
  cellSize: sized(CELL_SIZE, options?.cellSize),
  cuttingWidth: sized(CUTTING_WIDTH, options?.cuttingWidth),
});

/** One square of lawn on the mower's own axes, and what the mower has done there. */
export interface CoverageCell {
  /** The cell's corner with the lowest x and y, in metres from the dock. */
  x: number;
  y: number;
  /** How many separate times the mower has cut it. */
  visits: number;
  /** When it was last cut. */
  last: number;
}

export interface CoverageGrid {
  /** The side of a cell, in metres. */
  cellSize: number;
  cells: CoverageCell[];
}

export interface GridOptions {
  cellSize: number;
  /** The width of the mower's cutting deck, in metres. */
  cuttingWidth: number;
}

// A mower at work reports every two seconds. After a silence this long, where it went in between is
// unknown, so positions either side of one are not joined: that leaves out a docked mower, which
// reports every five minutes from one spot. A cell cut again within this long is still being cut by
// the same pass: the deck takes a second or so to cross it, and a turn at the end of a lane comes
// straight back over the ground beside it.
const PASS_ENDS_AFTER_MS = 20_000;

/**
 * The mower's unbroken passes, oldest first: the runs of positions with no silence between them,
 * whichever Trail each belongs to. A position on its own is not a pass.
 */
export function passes(trails: Trail[]): TrailPoint[][] {
  const runs: TrailPoint[][] = [];
  for (const segment of trails.flatMap((t) => t.segments)) {
    segment.forEach((position, i) =>
      i > 0 && position.time - segment[i - 1].time <= PASS_ENDS_AFTER_MS
        ? runs.at(-1)!.push(position)
        : runs.push([position])
    );
  }
  return runs.filter((run) => run.length > 1).sort((a, b) => a[0].time - b[0].time);
}

// Cells are counted by column and row from the dock. No lawn is a million cells across.
const SPAN = 2 ** 21;
const keyOf = (column: number, row: number) => (column + SPAN / 2) * SPAN + row + SPAN / 2;
const cornerOf = (key: number, cellSize: number) => ({
  x: (Math.floor(key / SPAN) - SPAN / 2) * cellSize,
  y: ((key % SPAN) - SPAN / 2) * cellSize,
});

/**
 * Coverage as a grid: every cell the mower has cut. Cells lie on the mower's own axes, counted from
 * the dock, so the grid is the same wherever the Dock origin puts it and a cell stays where it is
 * as more Trail arrives.
 *
 * Cells are counted along the line between consecutive positions, with the cutting width swept
 * along it. Positions alone are too far apart: on a real Job they came every three quarters of a
 * metre, which leaves a grid full of holes.
 */
export function coverageGrid(trails: Trail[], { cellSize, cuttingWidth }: GridOptions): CoverageGrid {
  const cells = new Map<number, CoverageCell>();
  const reach = cuttingWidth / 2;
  const cut = (x: number, y: number, time: number) => {
    const [column, row] = [Math.floor(x / cellSize), Math.floor(y / cellSize)];
    for (let r = Math.floor((y - reach) / cellSize); r <= Math.floor((y + reach) / cellSize); r++) {
      for (let c = Math.floor((x - reach) / cellSize); c <= Math.floor((x + reach) / cellSize); c++) {
        // A cell is cut when its middle is under the deck. The cell the mower is in always is, or
        // cells wider than the deck would be missed.
        const under = Math.hypot((c + 0.5) * cellSize - x, (r + 0.5) * cellSize - y) <= reach;
        if (!under && (c !== column || r !== row)) {
          continue;
        }
        const key = keyOf(c, r);
        const cell = cells.get(key);
        if (!cell) {
          cells.set(key, { ...cornerOf(key, cellSize), visits: 1, last: time });
        } else {
          cell.visits += time - cell.last > PASS_ENDS_AFTER_MS ? 1 : 0;
          cell.last = time;
        }
      }
    }
  };
  // In time order: a visit is told from the one before it.
  for (const pass of passes(trails)) {
    for (let i = 1; i < pass.length; i++) {
      const [a, b] = [pass[i - 1], pass[i]];
      // Half a cell at a time, so the line cannot step over one.
      const steps = Math.max(1, Math.ceil(Math.hypot(b.x - a.x, b.y - a.y) / (cellSize / 2)));
      for (let k = 0; k <= steps; k++) {
        cut(a.x + ((b.x - a.x) * k) / steps, a.y + ((b.y - a.y) * k) / steps, a.time + ((b.time - a.time) * k) / steps);
      }
    }
  }
  return { cellSize, cells: [...cells.values()] };
}

// A smoothed count this far below one visit is the far edge of the blur, not lawn.
export const SMOOTHED_FLOOR = 0.05;

/**
 * The grid's counts, each spread evenly over itself and the eight cells around it, twice: the
 * surface a heatmap would be if it could stand up.
 */
export function smoothed({ cells, cellSize }: CoverageGrid): Array<{ x: number; y: number; visits: number }> {
  let counts = new Map(cells.map((c) => [keyOf(Math.round(c.x / cellSize), Math.round(c.y / cellSize)), c.visits]));
  for (let pass = 0; pass < 2; pass++) {
    const spread = new Map<number, number>();
    for (const [key, count] of counts) {
      for (const beside of [-SPAN - 1, -SPAN, -SPAN + 1, -1, 0, 1, SPAN - 1, SPAN, SPAN + 1]) {
        spread.set(key + beside, (spread.get(key + beside) ?? 0) + count / 9);
      }
    }
    counts = spread;
  }
  return [...counts]
    .filter(([, visits]) => visits >= SMOOTHED_FLOOR)
    .map(([key, visits]) => ({ ...cornerOf(key, cellSize), visits }));
}
