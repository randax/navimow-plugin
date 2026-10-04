// Type-only import: this module never loads the map library, so it stays testable without a browser.
import type { RasterDEMSourceSpecification, RasterSourceSpecification, StyleSpecification } from 'maplibre-gl';
import type { Overlay } from './overlay';
import type { TrailScene } from './trail';

/** The tile sources the map is drawn from. Without a Terrain it is flat. */
export interface MapSources {
  baseMap: RasterSourceSpecification;
  overlay?: Overlay;
  terrain?: RasterDEMSourceSpecification;
}

/**
 * The whole map as one style, drawn bottom to top: Base map, Overlay, Trail. The Trail is part of
 * the style, so a Base map switch keeps it and a refresh only diffs its data. So is the Terrain:
 * the terrain prototype found that enabling it on a map already drawn leaves the camera at its
 * height above sea level, throwing the view outward by the height of the ground.
 */
export const mapStyle = (
  { baseMap, overlay, terrain }: MapSources,
  trail: TrailScene['lines']
): StyleSpecification => ({
  version: 8,
  sources: {
    base: baseMap,
    ...(overlay && { overlay: overlay.source }),
    ...(terrain && { terrain }),
    trail: { type: 'geojson', data: trail },
  },
  layers: [
    { id: 'base', type: 'raster', source: 'base' },
    ...(overlay
      ? [{ id: 'overlay', type: 'raster' as const, source: 'overlay', paint: { 'raster-opacity': overlay.opacity } }]
      : []),
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
