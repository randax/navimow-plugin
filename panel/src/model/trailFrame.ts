import type { DataFrame } from '@grafana/data';
import { STALE_AFTER_MS } from './recency';

/** Which column holds each value. Defaults follow the collector's schema. */
export interface TrailColumns {
  time: string;
  x: string;
  y: string;
  heading: string;
  job: string;
  zone: string;
  status: string;
  mower: string;
}

export const DEFAULT_TRAIL_COLUMNS: TrailColumns = {
  time: 'time',
  x: 'x',
  y: 'y',
  heading: 'theta',
  job: 'job_id',
  zone: 'zone',
  status: 'status',
  mower: 'device_id',
};

/** One position in the mower's local frame: metres from the dock, heading in radians from its x-axis. */
export interface TrailPoint {
  time: number;
  x: number;
  y: number;
  heading?: number;
  zone?: string;
  status?: string;
  mower?: string;
}

/**
 * One Job's positions in time order, as the unbroken runs between gaps in the data. `job` is absent
 * when the data has no Job column, or for positions recorded outside any Job.
 */
export interface Trail {
  job?: string;
  segments: TrailPoint[][];
}

const REQUIRED = ['time', 'x', 'y'] as const;

// Longer than a docked mower's 5-minute heartbeat, so a charging break inside a Job stays one line,
// and the same span after which a position counts as stale.
const GAP_MS = STALE_AFTER_MS;

const toNumber = (v: unknown): number =>
  typeof v === 'number' ? v : typeof v === 'string' && v.trim() !== '' ? Number(v) : NaN;

// Epoch milliseconds passed 1e11 in 1973, and epoch seconds will not reach it for three thousand years.
const toEpochMs = (n: number): number => (Math.abs(n) < 1e11 ? n * 1000 : n);

// Grafana time fields hold epoch milliseconds; a text column may hold epoch milliseconds or seconds, or a date. A date
// with a time of day but no zone is UTC, as Grafana reads SQL timestamps; Date.parse would take the
// browser's zone.
const toTime = (v: unknown): number => {
  if (typeof v !== 'string' || !Number.isNaN(Number(v))) {
    return toEpochMs(toNumber(v));
  }
  const text = v.trim();
  const zoneless = !/(Z|[+-]\d\d:?\d\d)$/i.test(text) && /\d:\d\d(:\d\d(\.\d+)?)?$/.test(text);
  return Date.parse(zoneless ? `${text.replace(' ', 'T')}Z` : text);
};

const toText = (v: unknown): string | undefined => (v === null || v === undefined || v === '' ? undefined : String(v));

const optionalNumber = (v: unknown): number | undefined => {
  const n = toNumber(v);
  return Number.isFinite(n) ? n : undefined;
};

const quoted = (names: string[]) => names.map((n) => `"${n}"`).join(' and ');

/**
 * Reads Trails from whatever frames the queries produced. Only time, x and y are required; any other
 * column that is missing means less to draw, never an error. Frames without the required columns are
 * someone else's query and are skipped, unless no frame has them.
 */
export function readTrails(
  frames: DataFrame[],
  overrides: Partial<TrailColumns> = {}
): { trails: Trail[] } | { problem: string } {
  // A cleared option comes back as an empty string; it means the default, not a column named "".
  const names = { ...DEFAULT_TRAIL_COLUMNS };
  for (const [key, name] of Object.entries(overrides) as Array<[keyof TrailColumns, string | undefined]>) {
    if (name?.trim()) {
      names[key] = name.trim();
    }
  }
  const values = (frame: DataFrame, column: keyof TrailColumns) =>
    frame.fields.find((f) => f.name === names[column])?.values;

  const missingFrom = (frame: DataFrame) => REQUIRED.filter((c) => !values(frame, c)).map((c) => names[c]);
  // A Trail query that returned no rows still has its columns: that is an empty range, not a mistake.
  const usable = frames.filter((f) => missingFrom(f).length === 0);
  if (usable.length === 0 && frames.some((f) => f.length > 0)) {
    // The frame closest to a Trail is the one the owner meant as the Trail query.
    const missing = frames.map(missingFrom).reduce((a, b) => (b.length < a.length ? b : a));
    return {
      problem:
        `No ${quoted(missing)} column${missing.length > 1 ? 's' : ''} for the Trail. ` +
        'Rename them in the query, or set which columns to use under Trail columns in the panel options.',
    };
  }

  const trails = new Map<string, Trail>();
  usable.forEach((frame, index) => {
    const [time, x, y, heading, job, zone, status, mower] = (
      ['time', 'x', 'y', 'heading', 'job', 'zone', 'status', 'mower'] as const
    ).map((c) => values(frame, c));

    // Every timed row in time order, since the mower delivers some positions seconds or hours late.
    // Rows without a position stay in: they mark where the line must break.
    const rows = [];
    for (let row = 0; row < frame.length; row++) {
      const t = toTime(time![row]);
      if (!Number.isFinite(t)) {
        continue;
      }
      const [mowerId, jobId] = [toText(mower?.[row]), toText(job?.[row])];
      const point: TrailPoint = {
        time: t,
        x: toNumber(x![row]),
        y: toNumber(y![row]),
        heading: optionalNumber(heading?.[row]),
        zone: toText(zone?.[row]),
        status: toText(status?.[row]),
        mower: mowerId,
      };
      rows.push({
        time: t,
        point: Number.isFinite(point.x) && Number.isFinite(point.y) ? point : undefined,
        mower: mowerId ?? '',
        jobId,
        // One Trail per Job of each mower; without a Job column, a frame is the only grouping on offer.
        key: job ? `job:${mowerId ?? ''}:${jobId ?? ''}` : `frame:${index}:${mowerId ?? ''}`,
      });
    }
    rows.sort((a, b) => a.time - b.time);

    // A line only continues from the same mower's previous row when that row is this Trail's own
    // position and not long ago, so a gap in the data is drawn as a gap.
    const previous = new Map<string, (typeof rows)[number]>();
    for (const row of rows) {
      const before = previous.get(row.mower);
      previous.set(row.mower, row);
      if (!row.point) {
        continue;
      }
      const trail = trails.get(row.key) ?? { job: row.jobId, segments: [] };
      trails.set(row.key, trail);
      if (before?.key === row.key && before.point && row.time - before.time <= GAP_MS) {
        trail.segments.at(-1)!.push(row.point);
      } else {
        trail.segments.push([row.point]);
      }
    }
  });

  return { trails: [...trails.values()].sort((a, b) => a.segments[0][0].time - b.segments[0][0].time) };
}
