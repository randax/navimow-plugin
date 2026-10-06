// Type-only imports: this module never loads the map library, so it stays testable without a browser.
import type { ExpressionSpecification, LayerSpecification } from 'maplibre-gl';
import type { Feature, FeatureCollection, LineString, MultiLineString, Point, Polygon } from 'geojson';
import { toLonLat, type DockOrigin } from './dockOrigin';
import { duration } from './recency';
import type { Trail, TrailPoint } from './trailFrame';

/** The three Coverage styles. */
export type CoverageStyle = 'grid' | 'heatmap' | 'line';

/** What colour and height tell: how often each part was cut, or how long ago. */
export type CoverageEncoding = 'visits' | 'age';

/** The styles and encodings, as the panel and the editor name them. */
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

interface CoverageProperties {
  /** How many times this was cut. Smoothed for a raised Heatmap, so not always whole. */
  visits?: number;
  /** Minutes between when this was last cut and the latest position of all. */
  age?: number;
  /** Where a step of the Trail comes among the steps drawn, from 0 for the oldest to 1 for the newest. */
  order?: number;
}

/** The layer that draws Coverage, less what the style adds: its id, its source and whether it is hidden. */
export type CoverageLayer = Pick<
  Extract<LayerSpecification, { type: 'fill' | 'fill-extrusion' | 'heatmap' | 'line' }>,
  'type' | 'paint' | 'layout'
>;

/** What the colours mean, for the panel to show beside the map. */
export interface CoverageLegend {
  title: string;
  /** The colours from one end to the other. */
  colours: string[];
  from: string;
  to: string;
}

/** Everything the map draws for Coverage, already in longitude and latitude. */
export interface CoverageScene {
  data: FeatureCollection<Polygon | Point | LineString | MultiLineString, CoverageProperties>;
  layer: CoverageLayer;
  legend: CoverageLegend;
  /** What this style cannot show of what was asked for, to tell the owner beside the map. */
  note?: string;
}

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
function passes(trails: Trail[]): TrailPoint[][] {
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
          cells.set(key, { x: c * cellSize, y: r * cellSize, visits: 1, last: time });
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

// Sequential ramps that stay clear of the Base map's greens. Visits run from few to many; time
// since mowed from just cut, in a cool dark blue, to longest ago, in the yellow of grass left to grow.
const RAMPS: Record<CoverageEncoding, string[]> = {
  visits: ['#F6C443', '#E4572E', '#5A0B4D'],
  age: ['#22306E', '#3D9BD9', '#FFD23F'],
};

// As tall as a person at the top of the scale: enough to read from across a garden, without hiding it.
const RAISED_METRES = [0.15, 2];

const RAISED: (colour: ExpressionSpecification, height: ExpressionSpecification) => CoverageLayer = (
  colour,
  height
) => ({
  type: 'fill-extrusion',
  paint: { 'fill-extrusion-color': colour, 'fill-extrusion-height': height, 'fill-extrusion-opacity': 0.85 },
});

/**
 * A size in metres on the ground, as pixels at each zoom. Pixels per metre double with every zoom
 * level, so one exponential ramp holds the size; MapLibre's world is 512 px wide at zoom 0.
 */
const metres = (size: number, { lat }: DockOrigin): ExpressionSpecification => {
  const atZoom0 = (size * 512) / (2 * Math.PI * 6378137 * Math.cos((lat * Math.PI) / 180));
  return ['interpolate', ['exponential', 2], ['zoom'], 0, atZoom0, 24, atZoom0 * 2 ** 24];
};

/**
 * A feature's value, or a stand-in where it has none. Changing style swaps the data and the layer,
 * and for a moment MapLibre draws the one with the other: shapes that lack what the layer reads.
 */
const value = (property: keyof CoverageProperties, missing: number): ExpressionSpecification => [
  'coalesce',
  ['get', property],
  missing,
];

/** How a value is drawn: the ends it runs between, and the colour, height and legend that follow. */
function scale(
  features: Array<{ properties: CoverageProperties }>,
  encoding: CoverageEncoding,
  from = encoding === 'visits' ? 1 : 0
) {
  const values = features.map((f) => f.properties[encoding] ?? 0).sort((a, b) => a - b);
  // One cell by the dock, crossed on every way out and back, must not flatten the rest of the lawn.
  const nearTop = values[Math.floor(values.length * 0.98)] ?? 0;
  const to = Math.max(nearTop, from + 1);
  const along = (outputs: Array<string | number>): ExpressionSpecification =>
    [
      'interpolate',
      ['linear'],
      value(encoding, from),
      ...outputs.flatMap((output, i) => [from + ((to - from) * i) / (outputs.length - 1), output]),
    ] as ExpressionSpecification;
  const legend: CoverageLegend =
    encoding === 'visits'
      ? { title: 'Visits', colours: RAMPS.visits, from: '1', to: `${Math.round(to)} or more` }
      : { title: 'Last mowed', colours: RAMPS.age, from: 'Latest', to: `${duration(to * 60_000)} earlier` };
  return { to, colour: along(RAMPS[encoding]), height: along(RAISED_METRES), legend };
}

type Placed<G extends Polygon | Point> = Feature<G, CoverageProperties>;

/** A square of lawn on the mower's axes, as the map draws it. */
const square = (
  origin: DockOrigin,
  { x, y }: { x: number; y: number },
  size: number,
  properties: CoverageProperties
): Placed<Polygon> => ({
  type: 'Feature',
  properties,
  geometry: {
    type: 'Polygon',
    coordinates: [
      [
        [x, y],
        [x + size, y],
        [x + size, y + size],
        [x, y + size],
        [x, y],
      ].map(([cornerX, cornerY]) => toLonLat(origin, cornerX, cornerY)),
    ],
  },
});

// A smoothed count this far below one visit is the far edge of the blur, not lawn.
const SMOOTHED_FLOOR = 0.05;

/**
 * The grid's counts, each spread evenly over itself and the eight cells around it, twice: the
 * surface a heatmap would be if it could stand up.
 */
function smoothed({ cells, cellSize }: CoverageGrid): Array<{ x: number; y: number; visits: number }> {
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
    .map(([key, visits]) => ({
      x: (Math.floor(key / SPAN) - SPAN / 2) * cellSize,
      y: ((key % SPAN) - SPAN / 2) * cellSize,
      visits,
    }));
}

const HEATMAP_LEGEND: CoverageLegend = { title: 'Visits', colours: RAMPS.visits, from: 'Few', to: 'Many' };

/**
 * A Heatmap is the grid seen out of focus. It is drawn from the grid's cells, not from the mower's
 * positions, so it is no heavier over a season than over a Job, and a mower standing still does not
 * burn a hole in it.
 */
function heatmap(grid: CoverageGrid, origin: DockOrigin, { raised, encoding }: CoverageSettings): CoverageScene {
  const { cells, cellSize } = grid;
  const note =
    encoding === 'age'
      ? 'A Heatmap shows how often each part was cut, not how long ago. Switch Coverage to Grid to see time since mowed.'
      : undefined;
  if (raised) {
    // MapLibre cannot raise a heatmap layer, so the smoothing is done here and drawn as columns.
    const features = smoothed(grid).map((cell) => square(origin, cell, cellSize, { visits: cell.visits }));
    const { colour, height } = scale(features, 'visits', SMOOTHED_FLOOR);
    return {
      data: { type: 'FeatureCollection', features },
      layer: RAISED(colour, height),
      legend: HEATMAP_LEGEND,
      note,
    };
  }
  const features = cells.map(({ x, y, visits }): Placed<Point> => ({
    type: 'Feature',
    properties: { visits },
    geometry: { type: 'Point', coordinates: toLonLat(origin, x + cellSize / 2, y + cellSize / 2) },
  }));
  // Each point is blurred over two cells' width. Across a lawn cut evenly, that adds up to 1.114
  // times a cell's own weight, so this puts the busiest cells at the top of the ramp.
  const radius = 2 * cellSize;
  const [few, some, many] = RAMPS.visits;
  return {
    data: { type: 'FeatureCollection', features },
    layer: {
      type: 'heatmap',
      paint: {
        'heatmap-weight': value('visits', 0),
        'heatmap-radius': metres(radius, origin),
        'heatmap-intensity': 1 / (1.114 * scale(features, 'visits').to),
        'heatmap-color': [
          'interpolate',
          ['linear'],
          ['heatmap-density'],
          0,
          'rgba(246, 196, 67, 0)',
          0.15,
          few,
          0.55,
          some,
          1,
          many,
        ],
        'heatmap-opacity': 0.8,
      },
    },
    legend: HEATMAP_LEGEND,
    note,
  };
}

// A Buffered line is a shape for every step the mower took, so it grows with the time range where
// the grid does not. This many is about five Jobs on a real lawn, and still quick to draw.
const BUFFERED_LINE_STEPS = 20_000;

/** The Trail drawn as wide as the mower cuts. */
function bufferedLine(
  trails: Trail[],
  origin: DockOrigin,
  { raised, encoding, cuttingWidth }: CoverageSettings
): CoverageScene | undefined {
  const all = passes(trails).flatMap((pass) => pass.slice(1).map((to, i) => ({ from: pass[i], to })));
  if (all.length === 0) {
    return undefined;
  }
  const steps = all.slice(-BUFFERED_LINE_STEPS);
  const [oldest, latest] = [steps[0].to.time, steps.at(-1)!.to.time];
  const count = (n: number) => n.toLocaleString('en-US');
  const notes = [
    all.length > steps.length &&
      `A Buffered line draws the latest ${count(steps.length)} of the ${count(all.length)} steps in this time range. ` +
        'Switch Coverage to Grid or Heatmap to see them all.',
    raised &&
      encoding === 'visits' &&
      'Raised, a Buffered line shows where the mower has cut, not how often. Switch Coverage to Grid to see visit count.',
  ].filter((text) => typeof text === 'string');
  const note = notes.length > 0 ? notes.join(' ') : undefined;
  const properties = ({ to }: (typeof steps)[number]): CoverageProperties => ({
    age: (latest - to.time) / 60_000,
    order: latest === oldest ? 1 : (to.time - oldest) / (latest - oldest),
  });
  const [, strong] = RAMPS.visits;
  const visited: CoverageLegend = {
    title: 'Visits',
    colours: ['rgba(228, 87, 46, 0.45)', strong],
    from: 'Once',
    to: 'More',
  };
  const place = (p: { x: number; y: number }) => toLonLat(origin, p.x, p.y);

  if (raised) {
    const half = cuttingWidth / 2;
    const features = steps.flatMap((step): Array<Placed<Polygon>> => {
      const { from, to } = step;
      const length = Math.hypot(to.x - from.x, to.y - from.y);
      if (length === 0) {
        return [];
      }
      // Half a width along the step, and the same across it.
      const [alongX, alongY] = [((to.x - from.x) / length) * half, ((to.y - from.y) / length) * half];
      const corners = [
        [from.x - alongX + alongY, from.y - alongY - alongX],
        [to.x + alongX + alongY, to.y + alongY - alongX],
        [to.x + alongX - alongY, to.y + alongY + alongX],
        [from.x - alongX - alongY, from.y - alongY + alongX],
      ];
      return [
        {
          type: 'Feature',
          properties: properties(step),
          geometry: {
            type: 'Polygon',
            coordinates: [[...corners, corners[0]].map(([x, y]) => toLonLat(origin, x, y))],
          },
        },
      ];
    });
    const { colour, legend } = scale(features, 'age');
    return {
      data: { type: 'FeatureCollection', features },
      layer: {
        type: 'fill-extrusion',
        paint: {
          'fill-extrusion-color': encoding === 'age' ? colour : strong,
          // Later passes stand a little higher, so the newest is the one seen where passes cross.
          'fill-extrusion-height': ['+', RAISED_METRES[0], ['*', 0.5, value('order', 0)]],
          'fill-extrusion-opacity': 0.85,
        },
      },
      legend: encoding === 'age' ? legend : { title: 'Mowed', colours: [strong, strong], from: '', to: '' },
      note,
    };
  }

  const layout = { 'line-cap': 'round' as const, 'line-join': 'round' as const };
  const width = metres(cuttingWidth, origin);
  if (encoding === 'visits') {
    // One line for all of it, see-through: MapLibre blends a line over itself, so ground crossed
    // more than once comes out darker.
    const parts = passes(trails).map((pass) => pass.map(place));
    const drawn = all.length > steps.length ? steps.map(({ from, to }) => [place(from), place(to)]) : parts;
    return {
      data: {
        type: 'FeatureCollection',
        features: [{ type: 'Feature', properties: {}, geometry: { type: 'MultiLineString', coordinates: drawn } }],
      },
      layer: { type: 'line', layout, paint: { 'line-width': width, 'line-color': strong, 'line-opacity': 0.45 } },
      legend: visited,
      note,
    };
  }
  // Drawn in order, so the newest pass is the one left showing.
  const features = steps.map((step): Feature<LineString, CoverageProperties> => ({
    type: 'Feature',
    properties: { age: properties(step).age },
    geometry: { type: 'LineString', coordinates: [place(step.from), place(step.to)] },
  }));
  const { colour, legend } = scale(features, 'age');
  return {
    data: { type: 'FeatureCollection', features },
    layer: { type: 'line', layout, paint: { 'line-width': width, 'line-color': colour } },
    legend,
    note,
  };
}

/**
 * What the map draws for Coverage, or nothing when the Trail has cut nothing. The Grid's two
 * encodings and the Heatmap all come from the one grid; the Buffered line from the same passes
 * the grid was counted along.
 */
export function coverageScene(
  trails: Trail[],
  origin: DockOrigin,
  settings: CoverageSettings
): CoverageScene | undefined {
  const { style, raised, encoding, cellSize, cuttingWidth } = settings;
  if (style === 'line') {
    return bufferedLine(trails, origin, settings);
  }
  const grid = coverageGrid(trails, { cellSize, cuttingWidth });
  if (grid.cells.length === 0) {
    return undefined;
  }
  if (style === 'heatmap') {
    return heatmap(grid, origin, settings);
  }
  const latest = grid.cells.reduce((time, cell) => Math.max(time, cell.last), -Infinity);
  const features = grid.cells.map((cell) =>
    square(origin, cell, cellSize, { visits: cell.visits, age: (latest - cell.last) / 60_000 })
  );
  const { colour, height, legend } = scale(features, encoding);
  return {
    data: { type: 'FeatureCollection', features },
    layer: raised
      ? RAISED(colour, height)
      : // Not antialiased: the hairline it draws around every cell shows as a mesh over the lawn.
        { type: 'fill', paint: { 'fill-color': colour, 'fill-opacity': 0.8, 'fill-antialias': false } },
    legend,
  };
}
