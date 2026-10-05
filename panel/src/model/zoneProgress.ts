import type { DataFrame } from '@grafana/data';
import { columnNames, toNumber, toText, toTime } from './trailFrame';

/** Which column holds each value of the optional Zone progress query. */
export interface ZoneProgressColumns {
  zone: string;
  progress: string;
  time: string;
}

export const DEFAULT_ZONE_PROGRESS_COLUMNS: ZoneProgressColumns = { zone: 'zone', progress: 'progress', time: 'time' };

/** How far through a Zone the mower is, as a percentage, and when that was reported. */
export interface ZoneProgress {
  progress: number;
  time?: number;
}

/** The latest progress of each Zone, by the identifier the mower gives it. */
export type ZoneProgressById = Record<string, ZoneProgress>;

/**
 * Reads the latest progress of each Zone from whatever frames the queries produced. A frame is the
 * Zone progress query's when it has a Zone and a progress column; any other is someone else's, the
 * Trail's included. The query is optional, so nothing here is ever a problem: a Zone without
 * progress is drawn as it was.
 */
export function readZoneProgress(frames: DataFrame[], overrides: Partial<ZoneProgressColumns> = {}): ZoneProgressById {
  const names = columnNames(DEFAULT_ZONE_PROGRESS_COLUMNS, overrides);
  const latest: ZoneProgressById = {};
  for (const frame of frames) {
    const [zone, progress, time] = (['zone', 'progress', 'time'] as const).map(
      (column) => frame.fields.find((f) => f.name === names[column])?.values
    );
    if (!zone || !progress) {
      continue;
    }
    for (let row = 0; row < frame.length; row++) {
      const [id, percent, at] = [toText(zone[row]), toNumber(progress[row]), toTime(time?.[row])];
      // Rows arrive in no promised order; one without a readable time is taken as newer than those before it.
      if (id === undefined || !Number.isFinite(percent) || at < (latest[id]?.time ?? -Infinity)) {
        continue;
      }
      latest[id] = { progress: Math.min(100, Math.max(0, percent)), ...(Number.isFinite(at) && { time: at }) };
    }
  }
  return latest;
}
