import type { DataFrame } from '@grafana/data';

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

/** One Job's positions in time order; `job` is absent when the data has no Job column. */
export interface Trail {
  job?: string;
  points: TrailPoint[];
}

const REQUIRED = ['time', 'x', 'y'] as const;

const toNumber = (v: unknown): number =>
  typeof v === 'number' ? v : typeof v === 'string' && v.trim() !== '' ? Number(v) : NaN;

// Grafana time fields hold epoch milliseconds; a text column may hold either that or an ISO date.
const toTime = (v: unknown): number => (typeof v === 'string' && Number.isNaN(Number(v)) ? Date.parse(v) : toNumber(v));

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

  const withRows = frames.filter((f) => f.length > 0);
  const usable = withRows.filter((f) => REQUIRED.every((c) => values(f, c)));
  if (usable.length === 0 && withRows.length > 0) {
    const missing = REQUIRED.filter((c) => !values(withRows[0], c)).map((c) => names[c]);
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
    for (let row = 0; row < frame.length; row++) {
      const point = { time: toTime(time![row]), x: toNumber(x![row]), y: toNumber(y![row]) };
      if (![point.time, point.x, point.y].every(Number.isFinite)) {
        continue;
      }
      const jobId = toText(job?.[row]);
      // Without a Job column, a frame is the only grouping the data offers.
      const key = job ? `job:${jobId ?? ''}` : `frame:${index}`;
      if (!trails.has(key)) {
        trails.set(key, { job: jobId, points: [] });
      }
      trails.get(key)!.points.push({
        ...point,
        heading: optionalNumber(heading?.[row]),
        zone: toText(zone?.[row]),
        status: toText(status?.[row]),
        mower: toText(mower?.[row]),
      });
    }
  });

  // The mower delivers some positions seconds or hours late, so arrival order is not time order.
  const result = [...trails.values()];
  result.forEach((t) => t.points.sort((a, b) => a.time - b.time));
  return { trails: result.sort((a, b) => a.points[0].time - b.points[0].time) };
}
