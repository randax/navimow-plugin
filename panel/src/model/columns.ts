/** Reading query columns: which column holds a value, and what a cell of it means. */

/** The name of each column: the default, unless the options name another. */
export function columnNames<T extends { [K in keyof T]: string }>(defaults: T, overrides: Partial<T> = {}): T {
  const names = { ...defaults };
  for (const key of Object.keys(defaults) as Array<keyof T>) {
    // A cleared option comes back as an empty string; it means the default, not a column named "".
    const name = overrides[key]?.trim();
    if (name) {
      names[key] = name as T[keyof T];
    }
  }
  return names;
}

export const toNumber = (v: unknown): number =>
  typeof v === 'number' ? v : typeof v === 'string' && v.trim() !== '' ? Number(v) : NaN;

// Epoch milliseconds passed 1e11 in 1973, and epoch seconds will not reach it for three thousand years.
const toEpochMs = (n: number): number => (Math.abs(n) < 1e11 ? n * 1000 : n);

// Grafana time fields hold epoch milliseconds; a text column may hold epoch milliseconds or seconds, or a date. A date
// with a time of day but no zone is UTC, as Grafana reads SQL timestamps; Date.parse would take the
// browser's zone.
export const toTime = (v: unknown): number => {
  if (typeof v !== 'string' || !Number.isNaN(Number(v))) {
    return toEpochMs(toNumber(v));
  }
  const text = v.trim();
  const zoneless = !/(Z|[+-]\d\d:?\d\d)$/i.test(text) && /\d:\d\d(:\d\d(\.\d+)?)?$/.test(text);
  return Date.parse(zoneless ? `${text.replace(' ', 'T')}Z` : text);
};

export const toText = (v: unknown): string | undefined =>
  v === null || v === undefined || v === '' ? undefined : String(v);
