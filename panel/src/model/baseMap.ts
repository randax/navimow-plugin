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

/** A built-in, named source, as a picker offers it. */
export interface Preset<S> {
  label: string;
  description?: string;
  source: S;
}

/** A Preset's source, or the problem to show for an id this version does not know. */
export const presetSource = <S>(
  slot: string,
  presets: Record<string, Preset<S>>,
  id: string
): { source: S } | { problem: string } =>
  Object.hasOwn(presets, id)
    ? { source: presets[id].source }
    : { problem: `Unknown ${slot} "${id}". Choose another under ${slot} in the panel options.` };

export const KARTVERKET_ATTRIBUTION = '<a href="https://www.kartverket.no/">© Kartverket</a>';

// Kartverket's WMTS orders the path row before column: {z}/{y}/{x}.
const kartverket = (layer: string): RasterSourceSpecification => ({
  type: 'raster',
  tiles: [`https://cache.kartverket.no/v1/wmts/1.0.0/${layer}/default/webmercator/{z}/{y}/{x}.png`],
  tileSize: 256,
  maxzoom: 18,
  attribution: KARTVERKET_ATTRIBUTION,
});

export const BASE_MAP_PRESETS: Record<BaseMapPreset, Preset<RasterSourceSpecification>> = {
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

/** A custom slot: what it is called, what it accepts, and what stands in for numbers left blank. */
export interface CustomSlot {
  name: string;
  /** Whether a WMS service will do as well as a tile service. */
  wms: boolean;
  tileSize: number;
  maxzoom: number;
}

export const CUSTOM_BASE_MAP: CustomSlot = { name: 'Base map', wms: true, tileSize: 256, maxzoom: 18 };

// MapLibre fills both kinds of template itself: {z}/{x}/{y} for tile services, {bbox-epsg-3857} for WMS.
const isTemplate = (url: string, wms: boolean): boolean =>
  ['{z}', '{x}', '{y}'].every((p) => url.includes(p)) || (wms && url.includes('{bbox-epsg-3857}'));

/** The URL as a browser would request it for one tile, if it is an http(s) URL a browser can parse. */
const filledIn = (template: string): URL | undefined => {
  try {
    const url = new URL(template.replace(/\{[^}]*\}/g, '0'));
    return /^https?:$/.test(url.protocol) ? url : undefined;
  } catch {
    return undefined;
  }
};

/** Ranges for the custom slot's numbers, shared with the option editor. */
export const TILE_SIZE = { min: 64, max: 1024 };
export const MAX_ZOOM = { min: 0, max: 24 };

const inRange = (value: number, { min, max }: { min: number; max: number }): boolean =>
  Number.isInteger(value) && value >= min && value <= max;

/** What every custom slot comes to, whichever kind of source it then becomes. */
export type CustomTiles = Required<Pick<RasterSourceSpecification, 'tiles' | 'tileSize' | 'maxzoom' | 'attribution'>>;

/** Checks a custom slot, so that what is wrong with it is shown in the panel rather than logged by the map. */
export function customTiles(
  slot: CustomSlot,
  { url = '', tileSize = slot.tileSize, maxzoom = slot.maxzoom, attribution = '' }: CustomSourceOptions = {}
): CustomTiles | { problem: string } {
  const request = isTemplate(url, slot.wms) ? filledIn(url.trim()) : undefined;
  if (!request) {
    return {
      problem: slot.wms
        ? `A custom ${slot.name} needs an http(s) URL containing either {z}, {x} and {y}, or {bbox-epsg-3857} for a WMS service.`
        : `A custom ${slot.name} needs an http(s) URL containing {z}, {x} and {y}.`,
    };
  }
  if (request.username || request.password) {
    return {
      problem: `A custom ${slot.name} URL cannot carry a user name or password before its host: browsers refuse to request it.`,
    };
  }
  if (!attribution.trim()) {
    return { problem: `A custom ${slot.name} needs an attribution. Enter the credit line its provider requires.` };
  }
  // The editor clamps these, but panel JSON does not; a tile size of 0 would request every tile at max zoom.
  if (!inRange(tileSize, TILE_SIZE)) {
    return { problem: `Tile size must be a whole number of pixels from ${TILE_SIZE.min} to ${TILE_SIZE.max}.` };
  }
  if (!inRange(maxzoom, MAX_ZOOM)) {
    return { problem: `Max zoom must be a whole number from ${MAX_ZOOM.min} to ${MAX_ZOOM.max}.` };
  }
  return { tiles: [url.trim()], tileSize, maxzoom, attribution: escapeHtml(attribution.trim()) };
}

/** A custom Base map or Overlay: both are drawn as plain raster tiles. */
export function customRaster(slot: CustomSlot, custom?: CustomSourceOptions): ResolvedBaseMap {
  const tiles = customTiles(slot, custom);
  return 'problem' in tiles ? tiles : { source: { type: 'raster', ...tiles } };
}

// Saved panels can outlive a preset, or predate these options entirely.
export const resolveBaseMap = ({ preset, custom }: BaseMapOptions = { preset: 'kartverket-topo' }): ResolvedBaseMap =>
  preset === 'custom' ? customRaster(CUSTOM_BASE_MAP, custom) : presetSource('Base map', BASE_MAP_PRESETS, preset);
