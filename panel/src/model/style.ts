// Type-only import: this module never loads the map library, so it stays testable without a browser.
import type {
  ExpressionSpecification,
  LayerSpecification,
  RasterDEMSourceSpecification,
  RasterSourceSpecification,
  StyleSpecification,
} from 'maplibre-gl';
import type { CoverageScene } from './coverage';
import type { BoundaryFeatures } from './lawn';
import type { Overlay } from './overlay';
import type { TrailScene } from './trail';

/** The tile sources the map is drawn from. Without a Terrain it is flat. */
export interface MapSources {
  baseMap: RasterSourceSpecification;
  overlay?: Overlay;
  terrain?: RasterDEMSourceSpecification;
}

// Tiles are drawn as they are, not faded in. MapLibre sets a tile's opacity as it draws a frame and
// asks whether a fade is still running afterwards; when the fade's end falls between the two, the
// map comes to rest with its tiles part faded and stays so until something else makes it draw. Seen
// under software rendering, with every tile left at 38 % opacity.
const NO_FADE = { 'raster-fade-duration': 0 };

/** A Boundary with nothing drawn: the source is always there, so a first Zone only needs new data. */
export const NO_BOUNDARY: BoundaryFeatures = { type: 'FeatureCollection', features: [] };

// One green for the lawn, another for its Zones, both faint: the Boundary says where the grass is
// without covering the Base map's picture of it or competing with the Trail's colours.
const BOUNDARY_COLOUR: ExpressionSpecification = ['match', ['get', 'kind'], 'outline', '#2E7D32', '#00897B'];

const HAS_PROGRESS: ExpressionSpecification = ['has', 'progress'];

// Progress fills a Zone from a pale wash to a deep one, in the Zones' own teal. One hue that only
// darkens reads as more of the same thing, and never passes for one of the Trail's colours.
const PROGRESS_COLOUR: ExpressionSpecification = [
  'interpolate',
  ['linear'],
  ['get', 'progress'],
  0,
  '#B2DFDB',
  100,
  '#00695C',
];

// Passes lie closer together than the Trail's line is wide once the whole lawn is in view, so at
// full strength it paints over the Coverage beneath it. While Coverage is shown the Trail recedes
// to a faint hairline: still there to follow and to hover, without hiding what it is drawn on.
const FULL_TRAIL = { 'line-width': 2, 'line-opacity': 0.9 };
const FAINT_TRAIL = { 'line-width': 1, 'line-opacity': 0.35 };

/** What the owner can show and hide from the panel, in the order the panel offers them. */
const HIDEABLE = [
  { id: 'trail', label: 'Trail' },
  { id: 'coverage', label: 'Coverage' },
  { id: 'boundary', label: 'Boundary' },
] as const;

export type Hideable = (typeof HIDEABLE)[number]['id'];

/** What the panel offers to hide: only what it has something to draw for. */
export const hideable = (boundary: BoundaryFeatures, coverage?: CoverageScene) =>
  HIDEABLE.filter(
    ({ id }) => id === 'trail' || (id === 'coverage' ? coverage !== undefined : boundary.features.length > 0)
  );

/**
 * The whole map as one style, drawn bottom to top: Base map, Overlay, the Boundary's fill, Coverage,
 * the Boundary's outline, Trail. Coverage is how the lawn was cut and the Boundary's fill only where
 * it is, so the one covers the other; the outline and the Trail stay readable on top. The Trail
 * and Boundary are part of the style, so a Base map switch keeps them and a refresh only diffs their
 * data. So is the Terrain: the terrain prototype found that enabling it on a map already drawn
 * leaves the camera at its height above sea level, throwing the view outward by the height of the
 * ground. What the owner has hidden stays in the style, undrawn, so showing it again is a restyle
 * in place like any other.
 */
export const mapStyle = (
  { baseMap, overlay, terrain }: MapSources,
  trail: TrailScene['lines'],
  boundary: BoundaryFeatures = NO_BOUNDARY,
  hidden: readonly Hideable[] = [],
  coverage?: Pick<CoverageScene, 'data' | 'layer'> | false
): StyleSpecification => {
  const visibility = (drawn: Hideable) => ({
    visibility: hidden.includes(drawn) ? ('none' as const) : ('visible' as const),
  });
  return {
    version: 8,
    sources: {
      base: baseMap,
      ...(overlay && { overlay: overlay.source }),
      ...(terrain && { terrain }),
      boundary: { type: 'geojson', data: boundary },
      ...(coverage && { coverage: { type: 'geojson' as const, data: coverage.data } }),
      trail: { type: 'geojson', data: trail },
    },
    layers: [
      { id: 'base', type: 'raster', source: 'base', paint: NO_FADE },
      ...(overlay
        ? [
            {
              id: 'overlay',
              type: 'raster' as const,
              source: 'overlay',
              paint: { 'raster-opacity': overlay.opacity, ...NO_FADE },
            },
          ]
        : []),
      {
        id: 'boundary-fill',
        type: 'fill',
        source: 'boundary',
        layout: visibility('boundary'),
        paint: {
          'fill-color': ['case', HAS_PROGRESS, PROGRESS_COLOUR, BOUNDARY_COLOUR],
          'fill-opacity': ['case', HAS_PROGRESS, 0.55, 0.12],
        },
      },
      // Its layer is the style's own: a fill, a heatmap, a line or, raised, columns standing on the ground.
      ...(coverage
        ? [
            {
              id: 'coverage',
              source: 'coverage',
              ...coverage.layer,
              layout: { ...coverage.layer.layout, ...visibility('coverage') },
            } as LayerSpecification,
          ]
        : []),
      {
        id: 'boundary-line',
        type: 'line',
        source: 'boundary',
        layout: { 'line-join': 'round', ...visibility('boundary') },
        paint: { 'line-color': BOUNDARY_COLOUR, 'line-width': 2, 'line-dasharray': [2, 1.5] },
      },
      // A line layer is laid over the Terrain, so the Trail follows the ground.
      {
        id: 'trail',
        type: 'line',
        source: 'trail',
        layout: { 'line-join': 'round', 'line-cap': 'round', ...visibility('trail') },
        paint: {
          'line-color': ['get', 'colour'],
          ...(coverage && !hidden.includes('coverage') ? FAINT_TRAIL : FULL_TRAIL),
        },
      },
    ],
    ...(terrain && { terrain: { source: 'terrain' } }),
  };
};
