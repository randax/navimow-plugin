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

// Saved options are JSON that provisioning or a hand may have written, so a field can hold anything.
// What is not text is no text.
const text = (value: unknown): string => (typeof value === 'string' ? value.trim() : '');

/**
 * A saved number: itself, or a plain decimal numeral that spells it, as provisioning files sometimes
 * hold. Absent, null or blank is undefined, for a default to stand in. Anything else is not a
 * number (NaN), so that it is refused by name rather than quietly replaced.
 */
export const numberFrom = (value: unknown): number | undefined => {
  if (typeof value === 'number') {
    return value;
  }
  if (value === undefined || value === null || (typeof value === 'string' && value.trim() === '')) {
    return undefined;
  }
  return /^[+-]?(\d+\.?\d*|\.\d+)$/.test(text(value)) ? Number(text(value)) : Number.NaN;
};

// MapLibre fills both kinds of template itself: {z}/{x}/{y} for tile services, {bbox-epsg-3857} for WMS.
const BBOX = '{bbox-epsg-3857}';
const isTemplate = (url: string, wms: boolean): boolean =>
  ['{z}', '{x}', '{y}'].every((p) => url.includes(p)) || (wms && url.includes(BBOX));

// Every placeholder MapLibre fills in a tile URL. Any other is requested as written, and in the host
// or path that draws nothing. The query is left alone: a filter value there may hold braces of its own.
const placeholders = (slot: CustomSlot): string[] => [
  ...['{z}', '{x}', '{y}', '{quadkey}', '{prefix}', '{ratio}'],
  ...(slot.wms ? [BBOX] : []),
];
const list = (items: string[]): string =>
  [items.slice(0, -1).join(', '), ...items.slice(-1)].filter(Boolean).join(' and ');

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
export function customTiles(slot: CustomSlot, custom?: CustomSourceOptions | null): CustomTiles | { problem: string } {
  const url = text(custom?.url);
  const attribution = text(custom?.attribution);
  const tileSize = numberFrom(custom?.tileSize) ?? slot.tileSize;
  const maxzoom = numberFrom(custom?.maxzoom) ?? slot.maxzoom;
  const filled = placeholders(slot);
  const unfilled = [...new Set(url.split('?')[0].match(/\{[^}]*\}/g))].filter((p) => !filled.includes(p));
  if (isTemplate(url, slot.wms) && unfilled.length > 0) {
    return {
      problem: `A custom ${slot.name} URL has ${list(unfilled)}, which the map cannot fill in. It fills ${list(filled)}.`,
    };
  }
  const request = isTemplate(url, slot.wms) ? filledIn(url) : undefined;
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
  if (!attribution) {
    return { problem: `A custom ${slot.name} needs an attribution. Enter the credit line its provider requires.` };
  }
  // The editor clamps these, but panel JSON does not; a tile size of 0 would request every tile at max zoom.
  if (!inRange(tileSize, TILE_SIZE)) {
    return { problem: `Tile size must be a whole number of pixels from ${TILE_SIZE.min} to ${TILE_SIZE.max}.` };
  }
  if (!inRange(maxzoom, MAX_ZOOM)) {
    return { problem: `Max zoom must be a whole number from ${MAX_ZOOM.min} to ${MAX_ZOOM.max}.` };
  }
  return { tiles: [url], tileSize, maxzoom, attribution: escapeHtml(attribution) };
}

/** A custom Base map or Overlay: both are drawn as plain raster tiles. */
export function customRaster(slot: CustomSlot, custom?: CustomSourceOptions | null): ResolvedBaseMap {
  const tiles = customTiles(slot, custom);
  return 'problem' in tiles ? tiles : { source: { type: 'raster', ...tiles } };
}

// Saved panels can outlive a preset, or predate these options entirely; absent or null is the default.
export function resolveBaseMap(options?: BaseMapOptions | null): ResolvedBaseMap {
  const preset = options?.preset ?? 'kartverket-topo';
  return preset === 'custom'
    ? customRaster(CUSTOM_BASE_MAP, options?.custom)
    : presetSource('Base map', BASE_MAP_PRESETS, preset);
}
