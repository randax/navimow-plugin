import type { FeatureCollection, Polygon } from 'geojson';
import { metresPerDegree, toLonLat, type DockOrigin, type DockOriginOptions } from './dockOrigin';
import type { Ring } from './drawing';
import type { ZoneProgressById } from './zoneProgress';

/** A piece of the lawn the mower works through, under the identifier the mower's own app gives it. */
export interface Zone {
  id: string;
  name: string;
  ring: Ring;
}

/** The lawn's outline and its Zones, as drawn by the owner. For display only; never sent to the mower. */
export interface Boundary {
  outline?: Ring;
  zones?: Zone[];
}

/**
 * Everything the calibration drawer edits, and saves in one go: where the mower's frame sits on
 * Earth, and what the lawn looks like. One option so the drawer's Save is one change.
 */
export interface Lawn {
  dockOrigin?: DockOriginOptions;
  boundary?: Boundary;
}

/** The text an owner sees, copies and pastes: the option exactly as the panel will save it. */
export const lawnText = (lawn: Lawn): string => JSON.stringify(lawn, null, 2);

// Everything a Dock origin holds: what pasted text is read for, and what a save spells out.
const DOCK_ORIGIN_KEYS = ['lat', 'lon', 'rotation'] as const satisfies ReadonlyArray<keyof DockOrigin>;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

class Refused extends Error {}

const refuse = (what: string, must: string): never => {
  throw new Refused(`${what} must be ${must}.`);
};

const finite = (value: unknown, what: string): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : refuse(what, 'a number');

const optionalFinite = (value: unknown, what: string): number | undefined =>
  value === undefined ? undefined : finite(value, what);

const corner = (value: unknown, what: string): [number, number] => {
  if (!Array.isArray(value) || value.length !== 2) {
    return refuse(what, 'a [longitude, latitude] pair');
  }
  const [lon, lat] = [finite(value[0], what), finite(value[1], what)];
  if (Math.abs(lon) > 180 || Math.abs(lat) > 90) {
    return refuse(what, 'a longitude within ±180 and a latitude within ±90');
  }
  return [lon, lat];
};

// GeoJSON repeats the first corner at the end; the drawer never does. Both are read, so a ring
// copied out of any GIS tool pastes in.
const ring = (value: unknown, what: string): Ring => {
  if (!Array.isArray(value)) {
    return refuse(what, 'a list of [longitude, latitude] corners');
  }
  const corners = value.map((c, i) => corner(c, `${what}[${i}]`));
  const [first, last] = [corners[0], corners.at(-1)];
  const open =
    corners.length > 1 && last && first[0] === last[0] && first[1] === last[1] ? corners.slice(0, -1) : corners;
  return open.length >= 3 ? open : refuse(what, 'a ring of at least three corners');
};

const zone = (value: unknown, what: string): Zone => {
  if (!isRecord(value)) {
    return refuse(what, 'an object with id, name and ring');
  }
  const id = value.id === undefined ? '' : typeof value.id === 'number' ? String(value.id) : value.id;
  if (typeof id !== 'string') {
    return refuse(`${what}.id`, 'text');
  }
  const name = value.name === undefined ? (id && `Zone ${id}`) || '' : value.name;
  if (typeof name !== 'string') {
    return refuse(`${what}.name`, 'text');
  }
  return { id, name, ring: ring(value.ring, `${what}.ring`) };
};

const dockOrigin = (value: unknown): DockOriginOptions => {
  if (!isRecord(value)) {
    return refuse('dockOrigin', 'an object with lat, lon and rotation');
  }
  const origin: DockOriginOptions = {};
  for (const key of DOCK_ORIGIN_KEYS) {
    const number = optionalFinite(value[key], `dockOrigin.${key}`);
    if (number !== undefined) {
      origin[key] = number;
    }
  }
  return origin;
};

const boundary = (value: unknown): Boundary => {
  if (!isRecord(value)) {
    return refuse('boundary', 'an object with outline and zones');
  }
  const result: Boundary = {};
  if (value.outline !== undefined) {
    result.outline = ring(value.outline, 'boundary.outline');
  }
  if (value.zones !== undefined) {
    if (!Array.isArray(value.zones)) {
      return refuse('boundary.zones', 'a list of Zones');
    }
    result.zones = value.zones.map((z, i) => zone(z, `boundary.zones[${i}]`));
  }
  return result;
};

/**
 * Reads pasted text into a Lawn, or says what is wrong with it. Everything is checked: the text
 * comes from the owner's clipboard, and a bad value saved into the panel would show up only as a
 * map that will not draw.
 */
export function parseLawn(text: string): { lawn: Lawn } | { problem: string } {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (error) {
    return { problem: `This is not valid JSON: ${error instanceof Error ? error.message : String(error)}` };
  }
  try {
    if (!isRecord(value)) {
      return refuse('The text', 'a JSON object with dockOrigin and boundary');
    }
    const lawn: Lawn = {};
    if (value.dockOrigin !== undefined) {
      lawn.dockOrigin = dockOrigin(value.dockOrigin);
    }
    if (value.boundary !== undefined) {
      lawn.boundary = boundary(value.boundary);
    }
    return { lawn };
  } catch (error) {
    if (error instanceof Refused) {
      return { problem: error.message };
    }
    throw error;
  }
}

/**
 * How far from the dock the rotation handle sits along the mower's x-axis. A few mowing lanes: far
 * enough that a pixel of drag is a fraction of a degree, close enough to stay in view with the Trail.
 */
export const HANDLE_METRES = 25;

/** Where the rotation handle is drawn for a Dock origin. */
export const handlePosition = (origin: DockOrigin): [number, number] => toLonLat(origin, HANDLE_METRES, 0);

/**
 * The rotation that points the x-axis from the dock at the handle, in [0, 360). Dragging the handle
 * onto the dock itself gives no direction, so the rotation stays as it was.
 */
export function rotationTowards(origin: DockOrigin, [lon, lat]: [number, number]): number {
  const metres = metresPerDegree(origin.lat);
  const east = (lon - origin.lon) * metres.lon;
  const north = (lat - origin.lat) * metres.lat;
  if (east === 0 && north === 0) {
    return origin.rotation;
  }
  // atan2 is within ±180, so one turn added brings it into [0, 360).
  return ((Math.atan2(east, north) * 180) / Math.PI + 360) % 360;
}

/** What the map draws for a Boundary: the outline and each Zone as a labelled polygon. */
export type BoundaryFeatures = FeatureCollection<
  Polygon,
  { kind: 'outline'; label: string } | { kind: 'zone'; label: string; id: string; progress?: number }
>;

/** How a Zone is named to the owner: its name, with the mower's identifier wherever that says more. */
export const zoneLabel = ({ id, name }: Pick<Zone, 'id' | 'name'>): string =>
  name && name !== id ? (id ? `${name} (${id})` : name) : id;

const polygon = (open: Ring): Polygon => ({ type: 'Polygon', coordinates: [[...open, open[0]]] });

/** A Zone whose progress has been reported carries it, which is what colours it. */
export function boundaryFeatures(boundary: Boundary | undefined, progress: ZoneProgressById = {}): BoundaryFeatures {
  const { outline, zones = [] } = boundary ?? {};
  return {
    type: 'FeatureCollection',
    features: [
      ...(outline
        ? [
            {
              type: 'Feature' as const,
              properties: { kind: 'outline' as const, label: 'Outline' },
              geometry: polygon(outline),
            },
          ]
        : []),
      ...zones.map(({ id, name, ring }, place) => ({
        type: 'Feature' as const,
        // Its place in the list, as two Zones may share an identifier: a Zone in two pieces of grass.
        id: place,
        properties: {
          kind: 'zone' as const,
          label: zoneLabel({ id, name }),
          id,
          ...(progress[id] && { progress: progress[id].progress }),
        },
        geometry: polygon(ring),
      })),
    ],
  };
}

/** The identifier a new Zone starts with: the next number after those in use, as the mower's app counts. */
export function nextZoneId(zones: Zone[]): string {
  const numbers = zones.map((z) => Number(z.id)).filter((n) => Number.isInteger(n) && n > 0);
  return String(Math.max(0, ...numbers) + 1);
}

/** A Lawn with one value of its Dock origin set, or taken out. */
export function withDockOrigin(lawn: Lawn | undefined, key: keyof DockOrigin, value: number | undefined): Lawn {
  const { [key]: _, ...others } = lawn?.dockOrigin ?? {};
  return { ...lawn, dockOrigin: value === undefined ? others : { ...others, [key]: value } };
}

/**
 * A Lawn as it has to be handed to Grafana to be saved: with everything it does not have spelled
 * out as undefined. Grafana merges what is saved into the options from before, and takes a value
 * that is simply missing to mean no change, so a rotation or a Boundary left out would stay.
 */
export function spelledOut({ dockOrigin, boundary }: Lawn): Lawn {
  return {
    dockOrigin: Object.fromEntries(DOCK_ORIGIN_KEYS.map((key) => [key, dockOrigin?.[key]])),
    boundary: boundary && { outline: boundary.outline, zones: boundary.zones },
  };
}

/**
 * The key a panel for one mower saves its Lawn under: whichever mower its query returns. A Lawn is
 * saved by mower identifier, so that a panel showing several mowers, each on its own Lawn, could be
 * added without moving what any panel has saved; such a panel would fall back on this one.
 */
export const ANY_MOWER = '*';

/** Where the options hold this panel's Lawn, as the option editors address it. */
export const LAWN_PATH = `lawns.${ANY_MOWER}`;

/** Options as saved by any version of the panel. */
export interface LawnOptions {
  /** Each mower's Lawn, by mower identifier. Read through resolveLawn. */
  lawns?: Record<string, Lawn>;
  /** Where panels saved before Lawns were kept by mower hold theirs. */
  lawn?: Lawn;
  /** Where panels saved before the Boundary existed hold the Dock origin. */
  dockOrigin?: DockOriginOptions;
}

/**
 * Moves a Lawn saved by an earlier version to where it is kept now: under `lawns`, for any mower.
 * Earlier versions held it under `lawn`, and before the Boundary existed held only the Dock origin,
 * at the root. Once a Lawn is saved in the new place it is the whole Lawn, and the older places are
 * leftovers to drop: read again, they would bring back whatever the owner has since taken out.
 */
export function migrateLawn<T extends LawnOptions>(options: T): Omit<T, 'dockOrigin' | 'lawn'> {
  const { dockOrigin: atRoot, lawn: single, ...rest } = options;
  if (rest.lawns?.[ANY_MOWER] !== undefined || (atRoot === undefined && single === undefined)) {
    return rest;
  }
  const dockOrigin = single?.dockOrigin ?? atRoot;
  return { ...rest, lawns: { ...rest.lawns, [ANY_MOWER]: { ...single, ...(dockOrigin && { dockOrigin }) } } };
}

/**
 * The Lawn a panel's options hold, wherever they hold it. Grafana runs the migration handler only
 * for a panel saved under another plugin version, so a panel saved yesterday still reads from the
 * older places. This is what the panel and every editor of the Lawn read, and each editor saves the
 * whole Lawn it read, so the first save of any part moves all of it to the new place.
 */
export const resolveLawn = (options: LawnOptions): Lawn | undefined => migrateLawn(options).lawns?.[ANY_MOWER];
