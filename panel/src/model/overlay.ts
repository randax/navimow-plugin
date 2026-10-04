// Type-only import: this module never loads the map library, so it stays testable without a browser.
import type { RasterSourceSpecification } from 'maplibre-gl';
import { customTiles, KARTVERKET_ATTRIBUTION, type CustomSourceOptions } from './baseMap';

export type OverlayPreset = 'kartverket-hillshade';

export interface OverlayOptions {
  preset: 'none' | OverlayPreset | 'custom';
  custom?: CustomSourceOptions;
  /** From 0, invisible, to 1, hiding the Base map; absent means half. */
  opacity?: number;
}

export interface Overlay {
  source: RasterSourceSpecification;
  opacity: number;
}

/** An Overlay to draw, nothing when none is chosen, or a problem to show the owner instead of a map. */
export type ResolvedOverlay = { overlay?: Overlay } | { problem: string };

interface Preset {
  label: string;
  description: string;
  source: RasterSourceSpecification;
}

export const OVERLAY_PRESETS: Record<OverlayPreset, Preset> = {
  'kartverket-hillshade': {
    label: 'Kartverket hillshade',
    description: "Shaded relief from Norway's national elevation model.",
    source: {
      type: 'raster',
      // A WMS service: MapLibre fills in each tile's bounding box.
      tiles: [
        'https://wms.geonorge.no/skwms1/wms.hoyde-dtm?SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&LAYERS=DTM:skyggerelieff&STYLES=&CRS=EPSG:3857&BBOX={bbox-epsg-3857}&WIDTH=256&HEIGHT=256&FORMAT=image/png',
      ],
      tileSize: 256,
      maxzoom: 18,
      attribution: KARTVERKET_ATTRIBUTION,
    },
  },
};

export const DEFAULT_OVERLAY_OPACITY = 0.5;

// The slider keeps opacity in range, but panel JSON does not.
const drawnOpacity = (opacity = DEFAULT_OVERLAY_OPACITY): number =>
  Number.isFinite(opacity) ? Math.min(1, Math.max(0, opacity)) : DEFAULT_OVERLAY_OPACITY;

export function resolveOverlay({ preset, custom, opacity }: OverlayOptions = { preset: 'none' }): ResolvedOverlay {
  if (preset === 'none') {
    return {};
  }
  if (preset !== 'custom') {
    const known = Object.hasOwn(OVERLAY_PRESETS, preset) ? OVERLAY_PRESETS[preset] : undefined;
    return known
      ? { overlay: { source: known.source, opacity: drawnOpacity(opacity) } }
      : { problem: `Unknown Overlay "${preset}". Choose another under Overlay in the panel options.` };
  }
  const tiles = customTiles('Overlay', custom, { tileSize: 256, maxzoom: 18 });
  return 'problem' in tiles
    ? tiles
    : { overlay: { source: { type: 'raster', ...tiles }, opacity: drawnOpacity(opacity) } };
}
