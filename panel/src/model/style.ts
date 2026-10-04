// Type-only import: this module never loads the map library, so it stays testable without a browser.
import type {
  ExpressionSpecification,
  RasterDEMSourceSpecification,
  RasterSourceSpecification,
  StyleSpecification,
} from 'maplibre-gl';
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

/**
 * The whole map as one style, drawn bottom to top: Base map, Overlay, Boundary, Trail. The Trail
 * and Boundary are part of the style, so a Base map switch keeps them and a refresh only diffs their
 * data. So is the Terrain: the terrain prototype found that enabling it on a map already drawn
 * leaves the camera at its height above sea level, throwing the view outward by the height of the
 * ground.
 */
export const mapStyle = (
  { baseMap, overlay, terrain }: MapSources,
  trail: TrailScene['lines'],
  boundary: BoundaryFeatures = NO_BOUNDARY
): StyleSpecification => ({
  version: 8,
  sources: {
    base: baseMap,
    ...(overlay && { overlay: overlay.source }),
    ...(terrain && { terrain }),
    boundary: { type: 'geojson', data: boundary },
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
      paint: { 'fill-color': BOUNDARY_COLOUR, 'fill-opacity': 0.12 },
    },
    {
      id: 'boundary-line',
      type: 'line',
      source: 'boundary',
      layout: { 'line-join': 'round' },
      paint: { 'line-color': BOUNDARY_COLOUR, 'line-width': 2, 'line-dasharray': [2, 1.5] },
    },
    // A line layer is laid over the Terrain, so the Trail follows the ground.
    {
      id: 'trail',
      type: 'line',
      source: 'trail',
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': ['get', 'colour'], 'line-width': 2, 'line-opacity': 0.9 },
    },
  ],
  ...(terrain && { terrain: { source: 'terrain' } }),
});
