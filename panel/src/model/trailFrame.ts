import type { DataFrame } from '@grafana/data';
import { columnNames, toNumber, toText, toTime } from './columns';
import { STALE_AFTER_MS } from './recency';
import { quoted } from './messages';

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
 * when the data has no Job column, or for positions recorded outside any Job, which are `outsideJob`.
 */
export interface Trail {
  job?: string;
  outsideJob?: boolean;
  segments: TrailPoint[][];
}

const REQUIRED = ['time', 'x', 'y'] as const;

// Longer than a docked mower's 5-minute heartbeat, so a charging break inside a Job stays one line,
// and the same span after which a position counts as stale.
const GAP_MS = STALE_AFTER_MS;

// No lawn reaches this far from its dock. Metres read from the wrong column do: a time column read as
// x puts the mower beyond the Moon, at a latitude no map can hold.
const MAX_METRES_FROM_DOCK = 10_000;

// A distance beyond the limit, for a message. Rounded up near the limit, so that a position just
// outside it is never said to be at it; far beyond, to the whole kilometre.
const kilometres = (metres: number): string =>
  (metres < 100_000 ? Math.ceil(metres / 100) / 10 : Math.round(metres / 1000)).toLocaleString('en-US');

const optionalNumber = (v: unknown): number | undefined => {
  const n = toNumber(v);
  return Number.isFinite(n) ? n : undefined;
};

/**
 * Reads Trails from whatever frames the queries produced. Only time, x and y are required; any other
 * column that is missing means less to draw, never an error. Frames without the required columns are
 * someone else's query and are skipped, unless no frame has them.
 */
export function readTrails(
  frames: DataFrame[],
  overrides: Partial<TrailColumns> = {}
): { trails: Trail[] } | { problem: string } {
  const names = columnNames(DEFAULT_TRAIL_COLUMNS, overrides);
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

  // Every timed row of every frame. Rows without a position stay in: they mark where a line must break.
  const rows = usable.flatMap((frame, index) => {
    const [time, x, y, heading, job, zone, status, mower] = (
      ['time', 'x', 'y', 'heading', 'job', 'zone', 'status', 'mower'] as const
    ).map((c) => values(frame, c));
    return Array.from({ length: frame.length }, (_, row) => {
      const [mowerId = '', jobId] = [toText(mower?.[row]), toText(job?.[row])];
      const point: TrailPoint = {
        time: toTime(time![row]),
        x: toNumber(x![row]),
        y: toNumber(y![row]),
        heading: optionalNumber(heading?.[row]),
        zone: toText(zone?.[row]),
        status: toText(status?.[row]),
        mower: toText(mower?.[row]),
      };
      const metres = Math.hypot(point.x, point.y);
      return {
        time: point.time,
        // NaN is no distance at all, so an unreadable position fails this too.
        point: metres <= MAX_METRES_FROM_DOCK ? point : undefined,
        metres,
        jobId,
        outsideJob: job !== undefined && jobId === undefined,
        // A Job column says which Trail a row belongs to, whichever frame it came in: Grafana splits
        // one series into frames by any text column, such as status or Zone. Without one, a frame is
        // the only grouping on offer, and its rows are a mower's history of their own.
        key: job ? `job:${mowerId}:${jobId ?? ''}` : `frame:${index}:${mowerId}`,
        history: job ? `job:${mowerId}` : `frame:${index}:${mowerId}`,
      };
    }).filter((r) => Number.isFinite(r.time));
  });
  // The mower delivers some positions seconds or hours late, so arrival order is not time order.
  rows.sort((a, b) => a.time - b.time);

  // A line only continues from the previous row of the same mower's history when that row is this
  // Trail's own position and not long ago, so a gap in the data is drawn as a gap.
  const trails = new Map<string, Trail>();
  const previous = new Map<string, (typeof rows)[number]>();
  for (const row of rows) {
    const before = previous.get(row.history);
    previous.set(row.history, row);
    if (!row.point) {
      continue;
    }
    const trail = trails.get(row.key) ?? { job: row.jobId, outsideJob: row.outsideJob || undefined, segments: [] };
    trails.set(row.key, trail);
    if (before?.key === row.key && before.point && row.time - before.time <= GAP_MS) {
      trail.segments.at(-1)!.push(row.point);
    } else {
      trail.segments.push([row.point]);
    }
  }

  // Rows that all came back unreadable, from a decimal comma or an unfamiliar date, would otherwise be
  // an empty map that looks like an empty range.
  if (trails.size === 0) {
    for (const column of REQUIRED) {
      const read = column === 'time' ? toTime : toNumber;
      const given = usable.flatMap((f) => Array.from(values(f, column)!)).filter((v) => toText(v) !== undefined);
      if (given.length > 0 && given.every((v) => !Number.isFinite(read(v)))) {
        return {
          problem:
            `The "${names[column]}" column has no value the Trail can read, such as "${String(given[0])}". ` +
            (column === 'time'
              ? 'Times must be epoch milliseconds or seconds, or dates like 2026-09-21T10:23:07Z.'
              : 'Positions must be numbers of metres, written with a decimal point.'),
        };
      }
    }
    // Readable, but nowhere near: the columns hold something other than metres from the dock.
    const far = rows.find((r) => r.metres > MAX_METRES_FROM_DOCK);
    if (far) {
      return {
        problem:
          `The ${quoted([names.x, names.y])} columns put every position more than ` +
          `${MAX_METRES_FROM_DOCK / 1000} km from the dock, the first ` +
          `${kilometres(far.metres)} km away. Positions must be metres ` +
          'from the dock: check which columns are set under Trail columns in the panel options.',
      };
    }
  }
  return { trails: [...trails.values()].sort((a, b) => a.segments[0][0].time - b.segments[0][0].time) };
}
