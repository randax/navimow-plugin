// Type-only import: this module never loads the map library, so it stays testable without a browser.
import type { RasterSourceSpecification } from 'maplibre-gl';
import {
  CUSTOM_BASE_MAP,
  customRaster,
  KARTVERKET_ATTRIBUTION,
  presetSource,
  type CustomSlot,
  type CustomSourceOptions,
  type Preset,
} from './baseMap';

export type OverlayPreset = 'kartverket-hillshade';

export interface OverlayOptions {
  preset?: 'none' | OverlayPreset | 'custom';
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

export const OVERLAY_PRESETS: Record<OverlayPreset, Preset<RasterSourceSpecification>> = {
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

export const CUSTOM_OVERLAY: CustomSlot = { ...CUSTOM_BASE_MAP, name: 'Overlay' };

// Absent means none, for the whole of the options or, in options written by hand, for the preset alone.
export function resolveOverlay({ preset = 'none', custom, opacity }: OverlayOptions = {}): ResolvedOverlay {
  if (preset === 'none') {
    return {};
  }
  const resolved =
    preset === 'custom' ? customRaster(CUSTOM_OVERLAY, custom) : presetSource('Overlay', OVERLAY_PRESETS, preset);
  return 'problem' in resolved ? resolved : { overlay: { source: resolved.source, opacity: drawnOpacity(opacity) } };
}
