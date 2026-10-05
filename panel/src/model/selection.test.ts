import { jobVariableName, selectedJobs } from './selection';

const holding = (value: unknown) => ({ current: { value } });

describe('selectedJobs', () => {
  test('a dashboard without the variable has no selection to make', () => {
    expect(selectedJobs(undefined)).toBeUndefined();
  });

  test('a variable holding one Job selects it', () => {
    expect(selectedJobs(holding('job-7'))).toEqual(['job-7']);
  });

  test('a variable holding several selects them all', () => {
    expect(selectedJobs(holding(['job-7', 'job-8']))).toEqual(['job-7', 'job-8']);
  });

  test('a numeric Job identifier is matched as text, as the Trail holds it', () => {
    expect(selectedJobs(holding(7))).toEqual(['7']);
  });

  test.each([
    ['All', '$__all'],
    ['All, as a list', ['$__all']],
    ['nothing yet', ''],
    ['no value', undefined],
  ])('a variable set to %s narrows nothing', (_, value) => {
    expect(selectedJobs(holding(value))).toEqual([]);
  });

  test('a variable with nothing current, such as one still loading, narrows nothing', () => {
    expect(selectedJobs({})).toEqual([]);
  });
});

describe('jobVariableName', () => {
  test('the option holds a reference to the variable, which is how Grafana knows to redraw the panel when it changes', () => {
    expect(jobVariableName('$mowing')).toBe('mowing');
    expect(jobVariableName('${mowing}')).toBe('mowing');
    expect(jobVariableName('${mowing:csv}')).toBe('mowing');
  });

  test('a panel saved without the option looks for a variable named job', () => {
    expect(jobVariableName(undefined)).toBe('job');
    expect(jobVariableName('  ')).toBe('job');
    expect(jobVariableName('${}')).toBe('job');
  });

  test('a bare name, as panel JSON written by hand may hold, is still the name', () => {
    expect(jobVariableName(' mowing ')).toBe('mowing');
  });
});
