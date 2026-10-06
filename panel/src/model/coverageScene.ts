// Type-only imports: this module never loads the map library, so it stays testable without a browser.
import type { ExpressionSpecification, LayerSpecification } from 'maplibre-gl';
import type {
  Feature,
  FeatureCollection,
  Geometry,
  LineString,
  MultiLineString,
  Point,
  Polygon,
  Position,
} from 'geojson';
import {
  coverageGrid,
  passes,
  smoothed,
  SMOOTHED_FLOOR,
  type CoverageEncoding,
  type CoverageGrid,
  type CoverageSettings,
} from './coverage';
import { toLonLat, type DockOrigin } from './dockOrigin';
import { duration, MIN } from './recency';
import type { Trail, TrailPoint } from './trailFrame';

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
  /** What each end stands for, where the colours run between two things. */
  ends?: [string, string];
}

/** Everything the map draws for Coverage, already in longitude and latitude. */
export interface CoverageScene {
  data: FeatureCollection<Polygon | Point | LineString | MultiLineString, CoverageProperties>;
  layer: CoverageLayer;
  legend: CoverageLegend;
  /** What this style cannot show of what was asked for, to tell the owner beside the map. */
  note?: string;
}

/** A style's part of the scene: the note is added by whoever knows what was asked for. */
type Drawn = Omit<CoverageScene, 'note'>;

// Sequential ramps that stay clear of the Base map's greens. Visits run from few to many; time
// since mowed from just cut, in a cool dark blue, to longest ago, in the yellow of grass left to grow.
const RAMPS: Record<CoverageEncoding, string[]> = {
  visits: ['#F6C443', '#E4572E', '#5A0B4D'],
  age: ['#22306E', '#3D9BD9', '#FFD23F'],
};

// As tall as a person at the top of the scale: enough to read from across a garden, without hiding it.
const RAISED_METRES = [0.15, 2];

/** Shapes standing up from the ground. */
const columns = (colour: ExpressionSpecification | string, height: ExpressionSpecification): CoverageLayer => ({
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
  const along = (outputs: Array<string | number>) =>
    [
      'interpolate',
      ['linear'],
      value(encoding, from),
      ...outputs.flatMap((output, i) => [from + ((to - from) * i) / (outputs.length - 1), output]),
    ] as ExpressionSpecification;
  const legend: CoverageLegend =
    encoding === 'visits'
      ? { title: 'Visits', colours: RAMPS.visits, ends: ['1', `${Math.round(to)} or more`] }
      : { title: 'Last mowed', colours: RAMPS.age, ends: ['Latest', `${duration(to * MIN)} earlier`] };
  return { to, colour: along(RAMPS[encoding]), height: along(RAISED_METRES), legend };
}

const collection = <G extends Geometry>(features: Array<Feature<G, CoverageProperties>>) => ({
  type: 'FeatureCollection' as const,
  features,
});

/** A shape on the mower's axes, given by its corners in metres, as the map draws it. */
const shape = (
  origin: DockOrigin,
  corners: number[][],
  properties: CoverageProperties
): Feature<Polygon, CoverageProperties> => ({
  type: 'Feature',
  properties,
  geometry: { type: 'Polygon', coordinates: [[...corners, corners[0]].map(([x, y]) => toLonLat(origin, x, y))] },
});

const square = (origin: DockOrigin, { x, y }: { x: number; y: number }, size: number, properties: CoverageProperties) =>
  shape(
    origin,
    [
      [x, y],
      [x + size, y],
      [x + size, y + size],
      [x, y + size],
    ],
    properties
  );

/** The lawn as square cells, each telling how often it was cut or how long ago. */
function gridCells(
  { cells, cellSize }: CoverageGrid,
  origin: DockOrigin,
  { raised, encoding }: CoverageSettings
): Drawn {
  const latest = cells.reduce((time, cell) => Math.max(time, cell.last), -Infinity);
  const features = cells.map((cell) =>
    square(origin, cell, cellSize, { visits: cell.visits, age: (latest - cell.last) / MIN })
  );
  const { colour, height, legend } = scale(features, encoding);
  return {
    data: collection(features),
    layer: raised
      ? columns(colour, height)
      : // Not antialiased: the hairline it draws around every cell shows as a mesh over the lawn.
        { type: 'fill', paint: { 'fill-color': colour, 'fill-opacity': 0.8, 'fill-antialias': false } },
    legend,
  };
}

const HEATMAP_LEGEND: CoverageLegend = { title: 'Visits', colours: RAMPS.visits, ends: ['Few', 'Many'] };

/**
 * A Heatmap is the grid seen out of focus. It is drawn from the grid's cells, not from the mower's
 * positions, so it is no heavier over a season than over a Job, and a mower standing still does not
 * burn a hole in it.
 */
function heatmap(grid: CoverageGrid, origin: DockOrigin, { raised }: CoverageSettings): Drawn {
  const { cells, cellSize } = grid;
  if (raised) {
    // MapLibre cannot raise a heatmap layer, so the smoothing is done here and drawn as columns.
    const features = smoothed(grid).map((cell) => square(origin, cell, cellSize, { visits: cell.visits }));
    const { colour, height } = scale(features, 'visits', SMOOTHED_FLOOR);
    return { data: collection(features), layer: columns(colour, height), legend: HEATMAP_LEGEND };
  }
  const features = cells.map(({ x, y, visits }): Feature<Point, CoverageProperties> => ({
    type: 'Feature',
    properties: { visits },
    geometry: { type: 'Point', coordinates: toLonLat(origin, x + cellSize / 2, y + cellSize / 2) },
  }));
  // Each point is blurred over two cells' width. Across a lawn cut evenly, that adds up to 1.114
  // times a cell's own weight, so this puts the busiest cells at the top of the ramp.
  const radius = 2 * cellSize;
  const [few, some, many] = RAMPS.visits;
  return {
    data: collection(features),
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
  };
}

// A Buffered line is a shape for every step the mower took, so it grows with the time range where
// the grid does not. This many is about five Jobs on a real lawn, and still quick to draw.
const BUFFERED_LINE_STEPS = 20_000;

const stepsIn = (drawn: TrailPoint[][]) => drawn.reduce((steps, pass) => steps + pass.length - 1, 0);

/** The latest steps of the passes, up to a limit: whole passes from the newest back, and the end of the one the limit falls in. */
function latest(all: TrailPoint[][], limit: number): TrailPoint[][] {
  const kept: TrailPoint[][] = [];
  for (let i = all.length - 1, room = limit; i >= 0 && room > 0; i--) {
    // As many steps as there is room for is one more position than that.
    const pass = all[i].slice(-(room + 1));
    kept.unshift(pass);
    room -= pass.length - 1;
  }
  return kept;
}

/** Each step of the passes, with how long before the newest it ended and where it comes among them. */
function steps(drawn: TrailPoint[][]) {
  const all = drawn.flatMap((pass) => pass.slice(1).map((to, i) => ({ from: pass[i], to })));
  const [oldest, newest] = [all[0].to.time, all.at(-1)!.to.time];
  return all.map((step) => ({
    ...step,
    age: (newest - step.to.time) / MIN,
    order: newest === oldest ? 1 : (step.to.time - oldest) / (newest - oldest),
  }));
}

const [, MOWED] = RAMPS.visits;
const ROUNDED = { 'line-cap': 'round' as const, 'line-join': 'round' as const };

/**
 * Flat, by visit count: one line for all of it, see-through. MapLibre blends a line over itself, so
 * ground crossed more than once comes out darker. Each pass is one unbroken part: drawn step by
 * step, the steps would overlap where they meet, and every position would read as ground cut twice.
 */
const blendedLine = (drawn: TrailPoint[][], origin: DockOrigin, { cuttingWidth }: CoverageSettings): Drawn => ({
  data: collection([
    {
      type: 'Feature',
      properties: {},
      geometry: {
        type: 'MultiLineString',
        coordinates: drawn.map((pass) => pass.map((p) => toLonLat(origin, p.x, p.y))),
      },
    },
  ]),
  layer: {
    type: 'line',
    layout: ROUNDED,
    paint: { 'line-width': metres(cuttingWidth, origin), 'line-color': MOWED, 'line-opacity': 0.45 },
  },
  legend: { title: 'Visits', colours: ['rgba(228, 87, 46, 0.45)', MOWED], ends: ['Once', 'More'] },
});

/** Flat, by time since mowed: a line for each step, drawn oldest first so the newest pass is the one left showing. */
function agedLine(drawn: TrailPoint[][], origin: DockOrigin, { cuttingWidth }: CoverageSettings): Drawn {
  const features = steps(drawn).map(({ from, to, age }): Feature<LineString, CoverageProperties> => ({
    type: 'Feature',
    properties: { age },
    geometry: { type: 'LineString', coordinates: [from, to].map((p): Position => toLonLat(origin, p.x, p.y)) },
  }));
  const { colour, legend } = scale(features, 'age');
  return {
    data: collection(features),
    layer: {
      type: 'line',
      layout: ROUNDED,
      paint: { 'line-width': metres(cuttingWidth, origin), 'line-color': colour },
    },
    legend,
  };
}

/** Raised: each step a slab as wide as the deck, run on by half a width at both ends so corners close. */
function slabs(drawn: TrailPoint[][], origin: DockOrigin, { encoding, cuttingWidth }: CoverageSettings): Drawn {
  const half = cuttingWidth / 2;
  const features = steps(drawn).flatMap(({ from, to, age, order }) => {
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
    return [shape(origin, corners, { age, order })];
  });
  const { colour, legend } = scale(features, 'age');
  // Later passes stand a little higher, so the newest is the one seen where passes cross.
  const height: ExpressionSpecification = ['+', RAISED_METRES[0], ['*', 0.5, value('order', 0)]];
  return {
    data: collection(features),
    layer: columns(encoding === 'age' ? colour : MOWED, height),
    legend: encoding === 'age' ? legend : { title: 'Mowed', colours: [MOWED, MOWED] },
  };
}

/** The Trail drawn as wide as the mower cuts, with what it had to leave out or cannot show. */
function bufferedLine(trails: Trail[], origin: DockOrigin, settings: CoverageSettings): CoverageScene | undefined {
  const all = passes(trails);
  if (all.length === 0) {
    return undefined;
  }
  const drawn = latest(all, BUFFERED_LINE_STEPS);
  const { raised, encoding } = settings;
  const count = (n: number) => n.toLocaleString('en-US');
  const notes = [
    stepsIn(all) > stepsIn(drawn) &&
      `A Buffered line draws the latest ${count(stepsIn(drawn))} of the ${count(stepsIn(all))} steps in this time range. ` +
        'Switch Coverage to Grid or Heatmap to see them all.',
    raised &&
      encoding === 'visits' &&
      'Raised, a Buffered line shows where the mower has cut, not how often. Switch Coverage to Grid to see visit count.',
  ].filter((text) => typeof text === 'string');
  const scene = (raised ? slabs : encoding === 'visits' ? blendedLine : agedLine)(drawn, origin, settings);
  return { ...scene, ...(notes.length > 0 && { note: notes.join(' ') }) };
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
  if (settings.style === 'line') {
    return bufferedLine(trails, origin, settings);
  }
  const grid = coverageGrid(trails, settings);
  if (grid.cells.length === 0) {
    return undefined;
  }
  if (settings.style === 'grid') {
    return gridCells(grid, origin, settings);
  }
  return {
    ...heatmap(grid, origin, settings),
    ...(settings.encoding === 'age' && {
      note: 'A Heatmap shows how often each part was cut, not how long ago. Switch Coverage to Grid to see time since mowed.',
    }),
  };
}
