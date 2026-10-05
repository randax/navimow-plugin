import { toLocal } from './dockOrigin';
import { zoneLabel, type Zone } from './lawn';
import type { TrailScene } from './trail';
import type { ZoneProgressById } from './zoneProgress';

/** What the panel tells about the thing under the pointer: a heading, and a row per fact. */
export interface Detail {
  title: string;
  /** The colour the thing is drawn in, where that tells it apart. */
  colour?: string;
  rows: Array<{ label: string; value: string }>;
}

/** What a detail is told with, beyond the Trail itself. */
export interface DetailContext {
  /** Times are told in the dashboard's time zone, which only the panel knows. */
  formatTime: (time: number) => string;
  zones?: Zone[];
  progress?: ZoneProgressById;
}

const named = (zones: Zone[] = [], id: string): string => {
  const zone = zones.find((z) => z.id === id);
  return zone ? zoneLabel(zone) : id;
};

/**
 * What to tell about a Trail where the pointer is: the time, Job, Zone and status of its position
 * nearest that place. `trail` is the id of the line under the pointer, so only that Trail is
 * searched and a neighbouring Job never answers for it.
 */
export function trailDetail(
  scene: TrailScene,
  trail: number,
  [lon, lat]: [number, number],
  { formatTime, zones }: DetailContext
): Detail | undefined {
  const found = scene.trails?.[trail];
  if (!found || !scene.origin) {
    return undefined;
  }
  // Compared on the mower's own axes, in metres, where near means near whatever the latitude.
  const [x, y] = toLocal(scene.origin, lon, lat);
  const away = (p: { x: number; y: number }) => (p.x - x) ** 2 + (p.y - y) ** 2;
  const nearest = found.segments.flat().reduce((a, b) => (away(b) < away(a) ? b : a));
  const job = found.outsideJob ? 'Outside any Job' : found.job;
  return {
    title: formatTime(nearest.time),
    colour: scene.lines.features.find((f) => f.id === trail)?.properties.colour,
    rows: [
      ...(job === undefined ? [] : [{ label: 'Job', value: job }]),
      ...(nearest.zone === undefined ? [] : [{ label: 'Zone', value: named(zones, nearest.zone) }]),
      ...(nearest.status === undefined ? [] : [{ label: 'Status', value: nearest.status }]),
    ],
  };
}

/** The Job a click on a Trail selects: none for positions outside any Job, or without a Job column. */
export const jobAt = (scene: TrailScene, trail: number): string | undefined => scene.trails?.[trail]?.job;

/** What to tell about a Zone of the Boundary: its name, and its latest progress when one is reported. */
export function zoneDetail(id: string, { formatTime, zones = [], progress = {} }: DetailContext): Detail | undefined {
  const zone = zones.find((z) => z.id === id);
  if (!zone) {
    return undefined;
  }
  const latest = progress[id];
  return {
    title: zoneLabel(zone),
    rows: latest
      ? [
          { label: 'Progress', value: `${Math.round(latest.progress)}%` },
          ...(latest.time === undefined ? [] : [{ label: 'Reported', value: formatTime(latest.time) }]),
        ]
      : [],
  };
}
