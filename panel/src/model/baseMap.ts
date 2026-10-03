// Type-only import: this module never loads the map library, so it stays testable without a browser.
import type { RasterSourceSpecification } from 'maplibre-gl';

export type BaseMapPreset = 'kartverket-topo' | 'kartverket-grey' | 'kartverket-toporaster' | 'osm';

export interface CustomSourceOptions {
  url?: string;
  tileSize?: number;
  maxzoom?: number;
  attribution?: string;
}

export interface BaseMapOptions {
  preset: BaseMapPreset | 'custom';
  custom?: CustomSourceOptions;
}

/** A source ready to draw, or a problem to show the owner instead of a map. */
export type ResolvedBaseMap = { source: RasterSourceSpecification } | { problem: string };

interface Preset {
  label: string;
  description?: string;
  source: RasterSourceSpecification;
}

const KARTVERKET_ATTRIBUTION = '<a href="https://www.kartverket.no/">© Kartverket</a>';

// Kartverket's WMTS orders the path row before column: {z}/{y}/{x}.
const kartverket = (layer: string): RasterSourceSpecification => ({
  type: 'raster',
  tiles: [`https://cache.kartverket.no/v1/wmts/1.0.0/${layer}/default/webmercator/{z}/{y}/{x}.png`],
  tileSize: 256,
  maxzoom: 18,
  attribution: KARTVERKET_ATTRIBUTION,
});

export const BASE_MAP_PRESETS: Record<BaseMapPreset, Preset> = {
  'kartverket-topo': { label: 'Kartverket topo', source: kartverket('topo') },
  'kartverket-grey': { label: 'Kartverket topo gråtone', source: kartverket('topograatone') },
  'kartverket-toporaster': { label: 'Kartverket turkart (toporaster)', source: kartverket('toporaster') },
  osm: {
    label: 'OpenStreetMap',
    description: "OpenStreetMap's tile usage policy allows light personal use only.",
    source: {
      type: 'raster',
      tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
      tileSize: 256,
      maxzoom: 19,
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    },
  },
};

// MapLibre renders attribution as HTML; a custom one is the panel editor's text, shown to every viewer.
const escapeHtml = (text: string): string =>
  text.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] ?? c);

// MapLibre fills both kinds of template itself: {z}/{x}/{y} for tile services, {bbox-epsg-3857} for WMS.
const isTileTemplate = (url: string): boolean =>
  /^https?:\/\//i.test(url) && (['{z}', '{x}', '{y}'].every((p) => url.includes(p)) || url.includes('{bbox-epsg-3857}'));

// Saved panels can outlive a preset, or predate these options entirely.
export function resolveBaseMap({ preset, custom }: BaseMapOptions = { preset: 'kartverket-topo' }): ResolvedBaseMap {
  if (preset !== 'custom') {
    const known = Object.hasOwn(BASE_MAP_PRESETS, preset) ? BASE_MAP_PRESETS[preset] : undefined;
    return known
      ? { source: known.source }
      : { problem: `Unknown Base map "${preset}". Choose another under Base map in the panel options.` };
  }
  const { url = '', tileSize = 256, maxzoom = 18, attribution = '' } = custom ?? {};
  if (!isTileTemplate(url.trim())) {
    return {
      problem:
        'A custom Base map needs an http(s) URL containing either {z}, {x} and {y}, or {bbox-epsg-3857} for a WMS service.',
    };
  }
  if (!attribution.trim()) {
    return { problem: 'A custom Base map needs an attribution. Enter the credit line its provider requires.' };
  }
  return {
    source: { type: 'raster', tiles: [url.trim()], tileSize, maxzoom, attribution: escapeHtml(attribution.trim()) },
  };
}
