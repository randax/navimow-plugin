import { FieldType, type DataFrame, type Field } from '@grafana/data';
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
const OTHER_THAN_X = ['time', 'y', 'heading', 'job', 'zone', 'status', 'mower'] as const;
// What Grafana's Time series format names the time field.
const TIME_SERIES_TIME = 'Time';

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

/** What a column holds in a row. */
type Column = (row: number) => unknown;

/** The rows of a frame that are one series' own, and its columns to read them by. */
interface Series extends Partial<Record<keyof TrailColumns, Column>> {
  time: Column;
  x: Column;
  y: Column;
  ownRows: number[];
  /** The query the series is a result of, or its frame when nothing says. */
  query: string | number;
}

// Fields are of one series when they carry the same labels, in whatever order.
const labelKey = (field: Field): string => JSON.stringify(Object.entries(field.labels ?? {}).sort());

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
  const fields = (frame: DataFrame, column: keyof TrailColumns): Field[] => {
    const named = frame.fields.filter((f) => f.name === names[column]);
    // The Time series format names the time field "Time", whatever the query called it.
    return named.length === 0 && column === 'time'
      ? frame.fields.filter((f) => f.name === TIME_SERIES_TIME && f.type === FieldType.time)
      : named;
  };

  const missingFrom = (frame: DataFrame) => REQUIRED.filter((c) => fields(frame, c).length === 0).map((c) => names[c]);
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

  /**
   * A frame as its series, each a set of columns. A Table frame is one series. Grafana's Time series
   * format turns text columns into labels and gives each set of labels number fields of its own, in
   * one frame with empty cells wherever a row is another set's: there a series is the fields with the
   * same labels, and a label is a column with one value.
   */
  const seriesOf = (frame: DataFrame, index: number): Series[] => {
    // Of the fields by a column's name, the one with the labels of a series: or the only one, as
    // the time is, which has no labels whichever series a row is of.
    const fieldOf = (column: keyof TrailColumns) => {
      const named = fields(frame, column);
      const byLabels = new Map(named.map((f) => [labelKey(f), f]));
      return (labels: string) => byLabels.get(labels) ?? (named.length === 1 ? named[0] : undefined);
    };
    const [time, y, heading, job, zone, status, mower] = OTHER_THAN_X.map(fieldOf);
    const positions = fields(frame, 'x');
    const xy = [...positions, ...fields(frame, 'y')];
    const all = Array.from({ length: frame.length }, (_, row) => row);
    // An empty cell in a frame of several series is another series' row, unless the row is empty
    // for all of them: then it is a row without a position, and nothing says whose.
    const empty = positions.length > 1 ? all.map((row) => xy.every((f) => f.values[row] == null)) : undefined;
    return positions.flatMap((x) => {
      const of = labelKey(x);
      const [timeField, yField] = [time(of), y(of)];
      if (!timeField || !yField) {
        return [];
      }
      const labels = new Map(Object.entries({ ...yField.labels, ...x.labels }));
      // A label is a column with one value, read when no field has the column's name.
      const column = (field: Field | undefined, name: string): Column | undefined => {
        const [values, label] = [field?.values, labels.get(name)];
        return values ? (row) => values[row] : label === undefined ? undefined : () => label;
      };
      const ownRows = empty
        ? all.filter((row) => empty[row] || x.values[row] != null || yField.values[row] != null)
        : all;
      return [
        {
          time: (row) => timeField.values[row],
          x: (row) => x.values[row],
          y: (row) => yField.values[row],
          heading: column(heading(of), names.heading),
          job: column(job(of), names.job),
          zone: column(zone(of), names.zone),
          status: column(status(of), names.status),
          mower: column(mower(of), names.mower),
          ownRows,
          // The frames of one query are one result, however Grafana split it.
          query: frame.refId ?? index,
        },
      ];
    });
  };
  const series = usable.flatMap(seriesOf);
  // Columns that are there, but as fields of different series, are a mistake and not an empty range.
  if (series.length === 0 && usable.some((f) => f.length > 0)) {
    return {
      problem:
        `No series has all of the ${quoted([names.time, names.x, names.y])} columns: they are fields with ` +
        'different labels. Check which columns are set under Trail columns in the panel options.',
    };
  }

  // Every timed row of every series. Rows without a position stay in: they mark where a line must break.
  const rows = series.flatMap(({ time, x, y, heading, job, zone, status, mower, ownRows, query }) =>
    ownRows
      .map((row) => {
        const [mowerId = '', jobId] = [toText(mower?.(row)), toText(job?.(row))];
        const point: TrailPoint = {
          time: toTime(time(row)),
          x: toNumber(x(row)),
          y: toNumber(y(row)),
          heading: optionalNumber(heading?.(row)),
          zone: toText(zone?.(row)),
          status: toText(status?.(row)),
          mower: toText(mower?.(row)),
        };
        const metres = Math.hypot(point.x, point.y);
        return {
          time: point.time,
          // NaN is no distance at all, so an unreadable position fails this too.
          point: metres <= MAX_METRES_FROM_DOCK ? point : undefined,
          metres,
          jobId,
          outsideJob: job !== undefined && jobId === undefined,
          // A Job column says which Trail a row belongs to, whichever frame or series it came in.
          // Without one, a query is the only grouping on offer, and its rows are a mower's history of
          // their own.
          key: job ? `job:${mowerId}:${jobId ?? ''}` : `query:${query}:${mowerId}`,
          history: job ? `job:${mowerId}` : `query:${query}:${mowerId}`,
        };
      })
      .filter((r) => Number.isFinite(r.time))
  );
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
      const given = series.flatMap((s) => s.ownRows.map(s[column])).filter((v) => toText(v) !== undefined);
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
