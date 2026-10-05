/**
 * The option's default: a reference to a variable named job. The option holds a reference, not a
 * bare name, because Grafana redraws a panel when a variable its options refer to changes; that is
 * what narrows the map the moment a Job is picked, without waiting for the next refresh.
 */
export const DEFAULT_JOB_VARIABLE = '$job';

/** The name of the variable the option refers to, written as $name or ${name}. */
export const jobVariableName = (option: string | undefined): string =>
  /\w+/.exec(option?.trim() || DEFAULT_JOB_VARIABLE)?.[0] ?? 'job';

// What Grafana holds in a variable set to All, whatever the options behind it.
const ALL = '$__all';

/**
 * The Jobs a dashboard's Job variable selects. Undefined when the dashboard has no such variable,
 * so there is nothing a click could set; empty when it has one that narrows nothing, as All does.
 * The variable is taken as Grafana hands it over: its kinds differ, and not all hold a value.
 */
export function selectedJobs(variable: object | undefined): string[] | undefined {
  if (!variable) {
    return undefined;
  }
  const current: unknown = 'current' in variable ? variable.current : undefined;
  const value: unknown =
    typeof current === 'object' && current !== null && 'value' in current ? current.value : undefined;
  const values = [value].flat().filter((v) => v !== undefined && v !== null && v !== '');
  return values.includes(ALL) ? [] : values.map(String);
}
